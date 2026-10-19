import argparse
import collections
import http.cookies
import json
import logging
import mimetypes
import os
import secrets
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .game import Problem, Store


ROOT = Path(__file__).resolve().parent.parent / 'static'
LOG = logging.getLogger('bingo')


class BingoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512

    def __init__(self, address, store, public_url='', secure=False):
        self.store = store
        self.public_url = public_url.rstrip('/')
        self.secure = secure
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(512)
        self.streams = threading.BoundedSemaphore(384)
        self.rate_lock = threading.Lock()
        self.attempts = collections.OrderedDict()
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def shutdown(self):
        self.stopping.set()
        with self.store.changed:
            self.store.changed.notify_all()
        super().shutdown()

    def rate_limit(self, bucket, ip, limit, window):
        key, now = (bucket, ip), time.monotonic()
        with self.rate_lock:
            attempts = self.attempts.setdefault(key, collections.deque())
            while attempts and attempts[0] < now - window:
                attempts.popleft()
            if len(attempts) >= limit:
                raise Problem('操作次數過多，請稍後再試。', 429)
            attempts.append(now)
            self.attempts.move_to_end(key)
            while len(self.attempts) > 10000:
                self.attempts.popitem(last=False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'DinnerBingo'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def log_message(self, fmt, *args):
        # Never log request bodies, ticket codes, passwords, or cookie values.
        LOG.debug('%s %s', self.client_address[0], fmt % args)

    def common_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')

    def send_json(self, value, status=200, cookie=None, download=None):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.common_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % download)
        self.end_headers()
        self.wfile.write(body)

    def token(self, role):
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except http.cookies.CookieError:
            return ''
        item = cookies.get('bingo_' + role)
        return item.value if item else ''

    def authenticate(self, role, csrf=False):
        session = self.server.store.session(self.token(role), role)
        if not session:
            raise Problem('登入已過期，請重新登入。' if role == 'admin' else '請使用原領卡碼恢復卡片。', 401)
        if csrf and not secrets.compare_digest(session['csrf'], self.headers.get('X-CSRF-Token', '')):
            raise Problem('操作驗證已過期，請重新整理。', 403)
        return session

    def cookie(self, role, token, clear=False):
        age = 0 if clear else (43200 if role == 'admin' else 604800)
        return 'bingo_%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s%s' % (
            role, token, age, '; Secure' if self.server.secure else '')

    def peer_ip(self):
        # Only the loopback proxy is trusted; Caddy overwrites incoming X-Forwarded-For.
        if self.server.public_url and self.client_address[0] in ('127.0.0.1', '::1'):
            return self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[-1].strip()
        return self.client_address[0]

    def read_body(self):
        if self.headers.get('Transfer-Encoding'):
            raise Problem('不支援此傳輸格式。', 400)
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/json':
            raise Problem('請使用 JSON 格式。', 415)
        origin = self.headers.get('Origin')
        expected = self.server.public_url or ('http://' + self.headers.get('Host', ''))
        if origin and origin != expected:
            raise Problem('不允許跨站操作。', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise Problem('不允許跨站操作。', 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise Problem('請求長度無效。')
        if not 0 < length <= 8192:
            raise Problem('請求資料過大或無效。', 413)
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise Problem('JSON 格式無效。')
        if not isinstance(value, dict):
            raise Problem('請求資料必須是物件。')
        return value

    @staticmethod
    def string(body, key):
        value = body.get(key, '')
        if not isinstance(value, str) or len(value) > 1024:
            raise Problem('欄位格式無效。')
        return value

    def do_GET(self):
        try:
            self.get()
        except Problem as e:
            self.send_json({'error': str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception:
            LOG.exception('GET failed')
            self.send_json({'error': '伺服器暫時無法處理，請稍後重試。'}, 500)

    def get(self):
        url = urlsplit(self.path)
        path, store = url.path, self.server.store
        if path == '/healthz':
            with store.connect() as db:
                db.execute('SELECT 1').fetchone()
            self.send_json({'ok': True})
        elif path == '/api/state':
            player = store.session(self.token('player'), 'player')
            data = store.snapshot(player['player_id'] if player else None)
            data['server_time'] = time.time()
            if player:
                data['csrf'] = player['csrf']
            self.send_json(data)
        elif path == '/api/admin/state':
            admin = self.authenticate('admin')
            query = parse_qs(url.query)
            try:
                gid = int(query['game'][0]) if 'game' in query else None
            except ValueError:
                raise Problem('局次資料無效。')
            data = store.snapshot(admin=True, game_id=gid)
            data['server_time'] = time.time()
            data['csrf'] = admin['csrf']
            data['public_url'] = self.server.public_url
            self.send_json(data)
        elif path == '/api/admin/export':
            self.authenticate('admin')
            self.send_json(store.export_all(), download='bingo-records.json')
        elif path == '/api/events':
            self.events()
        elif path.startswith('/api/'):
            raise Problem('找不到這個 API。', 404)
        else:
            routes = {'/': 'index.html', '/host': 'host.html', '/display': 'display.html'}
            target = ROOT / routes.get(path, path.lstrip('/'))
            if ROOT not in target.resolve().parents or not target.is_file():
                raise Problem('找不到此頁面。', 404)
            content = target.read_bytes()
            self.send_response(200)
            self.common_headers()
            mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
            self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') or mime == 'application/javascript' else ''))
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)

    def events(self):
        if not self.server.streams.acquire(blocking=False):
            raise Problem('即時連線已滿，稍後將自動重連。', 503)
        try:
            self.send_response(200)
            self.common_headers()
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache, no-transform')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            revision = -1
            started = time.monotonic()
            while not self.server.stopping.is_set() and time.monotonic() - started < 1800:
                with self.server.store.changed:
                    current = self.server.store.revision
                    if current == revision:
                        self.server.store.changed.wait(timeout=12)
                    current = self.server.store.revision
                if current != revision:
                    self.wfile.write(('id: %s\ndata: %s\n\n' % (current, json.dumps({'revision': current}))).encode())
                    revision = current
                else:
                    self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
        finally:
            self.server.streams.release()

    def do_POST(self):
        try:
            self.post()
        except Problem as e:
            self.close_connection = True
            self.send_json({'error': str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception:
            LOG.exception('POST failed')
            self.close_connection = True
            self.send_json({'error': '伺服器暫時無法處理，請重新整理確認進度。'}, 500)

    def post(self):
        path, store = urlsplit(self.path).path, self.server.store
        # Limit before parsing/hashing. NAT attendees share an IP, so claims have a higher ceiling.
        if path == '/api/login':
            self.server.rate_limit('login', self.peer_ip(), 8, 60)
        elif path == '/api/claim':
            self.server.rate_limit('claim', self.peer_ip(), 400, 60)
        else:
            self.server.rate_limit('write', self.token('admin') or self.token('player') or self.peer_ip(), 240, 60)
        body = self.read_body()
        cookie = None
        result = {'ok': True}
        if path == '/api/login':
            token = store.login(self.string(body, 'password'))
            cookie = self.cookie('admin', token)
        elif path == '/api/claim':
            existing = store.session(self.token('player'), 'player')
            token = store.claim(self.string(body, 'code'), self.string(body, 'name'), existing['player_id'] if existing else None)
            cookie = self.cookie('player', token)
        elif path == '/api/marks':
            player = self.authenticate('player', csrf=True)
            store.marks(player['player_id'], body.get('game_id'), body.get('marks'))
        elif path.startswith('/api/admin/'):
            self.authenticate('admin', csrf=True)
            if path == '/api/admin/logout':
                store.logout(self.token('admin'))
                cookie = self.cookie('admin', '', clear=True)
            elif path == '/api/admin/tickets':
                store.create_tickets(body.get('count'))
            elif path == '/api/admin/registration':
                store.registration(body.get('open'), body.get('game_id'))
            elif path == '/api/admin/draw':
                result['draw'] = store.draw(body.get('game_id'), body.get('expected_round'))
            elif path == '/api/admin/claimed':
                store.claimed(body.get('game_id'), body.get('player_id'), body.get('claimed'))
            elif path == '/api/admin/reset':
                result['game_id'] = store.reset(body.get('game_id'), self.string(body, 'confirmation'))
            else:
                raise Problem('找不到這個操作。', 404)
        else:
            raise Problem('找不到這個操作。', 404)
        self.send_json(result, cookie=cookie)


def main():
    parser = argparse.ArgumentParser(description='Dinner Bingo server (one process only)')
    parser.add_argument('--db', default=os.environ.get('BINGO_DB', 'data/bingo.sqlite3'))
    parser.add_argument('--host', default=os.environ.get('BINGO_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('BINGO_PORT', '8000')))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    public = os.environ.get('BINGO_PUBLIC_URL', '').rstrip('/')
    if public and (urlsplit(public).scheme != 'https' or not urlsplit(public).netloc or urlsplit(public).path):
        parser.error('BINGO_PUBLIC_URL 必須是 https://your-domain，且不含路徑。')
    store = Store(args.db)
    state = store.snapshot()
    if state['demo'] and (public or args.host not in ('127.0.0.1', 'localhost')):
        parser.error('示範資料庫只能在本機 loopback 使用，正式環境請另行 init。')
    server = BingoServer((args.host, args.port), store, public, secure=bool(public))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    LOG.info('Bingo ready: http://%s:%s | player / | host /host | projection /display', args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()


if __name__ == '__main__':
    main()
