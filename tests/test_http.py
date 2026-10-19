import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from bingo.game import Store
from bingo.server import BingoServer


class HTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / 'http.sqlite3')
        cls.store.initialize('http-password-123')
        cls.store.create_tickets(2)
        cls.store.registration(True, 1)
        cls.server = BingoServer(('127.0.0.1', 0), cls.store)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.tmp.cleanup()

    def request(self, path, body=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json'}
        if cookie: h['Cookie'] = cookie
        if headers: h.update(headers)
        conn.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, h)
        response = conn.getresponse(); raw = response.read()
        result = (response.status, raw, dict(response.getheaders()))
        conn.close(); return result

    def test_static_pages_csp_and_no_external_assets(self):
        for route in ['/', '/host', '/display', '/app.js', '/style.css', '/vendor/qrcode.js', '/draw-animation.mjs', '/draw-animation.css']:
            status, body, headers = self.request(route)
            self.assertEqual(status, 200)
            self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
            self.assertGreater(len(body), 0)
            if route == '/draw-animation.mjs':
                self.assertIn('javascript', headers['Content-Type'])
        self.assertEqual(self.request('/../manage.py')[0], 404)
        self.assertEqual(self.request('/healthz')[0], 200)

    def test_auth_csrf_origin_cookie_and_public_privacy(self):
        self.assertEqual(self.request('/api/admin/state')[0], 401)
        status, raw, headers = self.request('/api/login', {'password': 'http-password-123'})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        admin_cookie = headers['Set-Cookie'].split(';')[0]
        _, raw, _ = self.request('/api/admin/state', cookie=admin_cookie)
        admin = json.loads(raw); csrf = {'X-CSRF-Token': admin['csrf']}
        self.assertEqual(self.request('/api/admin/tickets', {'count': 1}, cookie=admin_cookie)[0], 403)
        self.assertEqual(self.request('/api/admin/tickets', {'count': 1}, cookie=admin_cookie,
                                      headers={**csrf, 'Origin': 'https://evil.example'})[0], 403)
        code = admin['tickets'][0]['code']
        status, _, headers = self.request('/api/claim', {'name': '<img src=x onerror=x>', 'code': code})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        _, raw, _ = self.request('/api/state', cookie=cookie)
        player = json.loads(raw)
        self.assertGreater(player['server_time'], 0)
        self.assertIn('card', player)
        self.assertNotIn('tickets', player)
        self.assertEqual(self.request('/api/admin/state', cookie=cookie)[0], 401)
        self.assertEqual(self.request('/api/marks', {'game_id': 1, 'marks': [0,24]}, cookie=cookie,
                                      headers={'X-CSRF-Token': player['csrf']})[0], 200)
        _, raw, _ = self.request('/api/state', cookie=cookie)
        self.assertEqual(json.loads(raw)['card']['marks'], [0,12,24])
        _, raw, _ = self.request('/api/state')
        public = json.loads(raw)
        self.assertNotIn('card', public); self.assertNotIn('csrf', public)
        self.assertNotIn('tickets', public); self.assertNotIn(code.encode(), raw)
        self.assertEqual(self.request('/api/admin/export', cookie=admin_cookie)[0], 200)

    def test_sse_reconnect_sends_current_revision(self):
        for _ in range(2):
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            conn.request('GET', '/api/events', headers={'Last-Event-ID': '0'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
            self.assertTrue(response.readline().startswith(b'id:'))
            self.assertIn(str(self.store.revision).encode(), response.readline())
            response.close(); conn.close()

    def test_invalid_body_is_a_client_error(self):
        self.assertEqual(self.request('/api/claim', {'name': [], 'code': 1})[0], 400)
        self.assertEqual(self.request('/api/login', {'password': 'wrong'})[0], 401)


if __name__ == '__main__':
    unittest.main()
