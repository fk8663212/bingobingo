import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path


LINES = ([list(range(r * 5, r * 5 + 5)) for r in range(5)]
         + [[c + r * 5 for r in range(5)] for c in range(5)]
         + [[0, 6, 12, 18, 24], [4, 8, 12, 16, 20]])


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000)
    return salt + ':' + value.hex()


def password_matches(password, stored):
    return secrets.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def make_card():
    values = secrets.SystemRandom().sample(range(1, 50), 24)
    values.insert(12, 0)
    return values


def completed_lines(card, drawn):
    return [line for line in LINES if all(card[i] == 0 or card[i] in drawn for i in line)]


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS games(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
 registration_open INTEGER NOT NULL DEFAULT 0, max_winners INTEGER NOT NULL DEFAULT 10,
 created_at REAL NOT NULL, ended_at REAL);
CREATE TABLE IF NOT EXISTS tickets(
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
 redeemed_at REAL);
CREATE TABLE IF NOT EXISTS players(
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id),
 name TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards(
 id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id),
 player_id INTEGER NOT NULL REFERENCES players(id), numbers TEXT NOT NULL,
 marks TEXT NOT NULL DEFAULT '[12]', qualified_round INTEGER,
 UNIQUE(game_id, player_id));
CREATE TABLE IF NOT EXISTS draws(
 game_id INTEGER NOT NULL REFERENCES games(id), round INTEGER NOT NULL,
 number INTEGER NOT NULL, drawn_at REAL NOT NULL, candidates TEXT NOT NULL,
 lottery_order TEXT NOT NULL, selected TEXT NOT NULL,
 PRIMARY KEY(game_id, round), UNIQUE(game_id, number));
CREATE TABLE IF NOT EXISTS winners(
 game_id INTEGER NOT NULL REFERENCES games(id), player_id INTEGER NOT NULL REFERENCES players(id),
 round INTEGER NOT NULL, position INTEGER NOT NULL, claimed_at REAL,
 PRIMARY KEY(game_id, player_id), UNIQUE(game_id, position));
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, player_id INTEGER REFERENCES players(id),
 csrf TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, game_id INTEGER, action TEXT NOT NULL,
 payload TEXT NOT NULL, created_at REAL NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO settings VALUES('revision', '0')")
            self.revision = int(db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0])

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self, notify=True):
        # One process only. BEGIN IMMEDIATE also protects against a concurrent CLI writer.
        with self.lock:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                yield db
                if notify:
                    db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                revision = int(db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0])
            if notify:
                with self.changed:
                    self.revision = revision
                    self.changed.notify_all()

    @staticmethod
    def current(db):
        row = db.execute('SELECT * FROM games ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            raise Problem('尚未初始化活動，請先執行 manage.py init。', 503)
        return row

    def initialize(self, password, title='導生晚宴', demo=False):
        if len(password) < 12:
            raise Problem('主持人密碼至少需要 12 個字元。')
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='password'").fetchone():
                raise Problem('資料庫已初始化；請使用 password 指令修改密碼。')
            db.execute("INSERT INTO settings VALUES('password', ?)", (password_hash(password),))
            db.execute("INSERT INTO settings VALUES('demo', ?)", ('1' if demo else '0',))
            db.execute('INSERT INTO games(title, created_at) VALUES(?, ?)', (title, time.time()))

    def change_password(self, password):
        if len(password) < 12:
            raise Problem('密碼至少需要 12 個字元。')
        with self.transaction() as db:
            db.execute("UPDATE settings SET value=? WHERE key='password'", (password_hash(password),))
            db.execute("DELETE FROM sessions WHERE role='admin'")

    @staticmethod
    def log(db, action, payload, game_id=None):
        db.execute('INSERT INTO audit(game_id,action,payload,created_at) VALUES(?,?,?,?)',
                   (game_id, action, json.dumps(payload, ensure_ascii=False), time.time()))

    @staticmethod
    def new_session(db, role, player_id=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        db.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                   (digest(token), role, player_id, csrf, time.time() + (43200 if role == 'admin' else 604800)))
        return token

    def session(self, token, role):
        if not token:
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE token_hash=? AND role=? AND expires_at>?',
                             (digest(token), role, time.time())).fetchone()
            return dict(row) if row else None

    def login(self, password):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='password'").fetchone()
        if not row or not password_matches(password, row[0]):
            raise Problem('主持人密碼不正確。', 401)
        with self.transaction(notify=False) as db:
            return self.new_session(db, 'admin')

    def logout(self, token):
        with self.transaction(notify=False) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))

    def create_tickets(self, count):
        if type(count) is not int or not 1 <= count <= 200:
            raise Problem('一次可產生 1～200 組領卡碼。')
        with self.transaction() as db:
            if db.execute('SELECT count(*) FROM tickets').fetchone()[0] + count > 1000:
                raise Problem('本活動最多保留 1,000 組領卡碼。')
            for _ in range(count):
                code = secrets.token_hex(8).upper()
                db.execute('INSERT INTO tickets(code,created_at) VALUES(?,?)', (code, time.time()))
            self.log(db, 'tickets_created', {'count': count})

    def claim(self, code, name, existing_player=None):
        code = ''.join(c for c in code.upper() if c not in '- ')
        if len(code) != 16 or any(c not in '0123456789ABCDEF' for c in code):
            raise Problem('請輸入完整的 16 碼領卡碼。')
        name = name.strip()
        if not 1 <= len(name) <= 24 or any(ord(c) < 32 for c in name):
            raise Problem('請輸入 1～24 字的稱呼。')
        with self.transaction() as db:
            game = self.current(db)
            ticket = db.execute('SELECT * FROM tickets WHERE code=?', (code,)).fetchone()
            if not ticket:
                raise Problem('找不到這組領卡碼，請向工作人員確認。', 404)
            player = db.execute('SELECT * FROM players WHERE ticket_id=?', (ticket['id'],)).fetchone()
            if existing_player and (not player or player['id'] != existing_player):
                raise Problem('此瀏覽器已有賓果卡，不能再領取另一張。', 409)
            if not player:
                if not game['registration_open'] or game['status'] != 'waiting':
                    raise Problem('目前未開放領卡。已領卡者仍可用原碼恢復。', 409)
                pid = db.execute('INSERT INTO players(ticket_id,name,created_at) VALUES(?,?,?)',
                                 (ticket['id'], name, time.time())).lastrowid
                db.execute('UPDATE tickets SET redeemed_at=? WHERE id=?', (time.time(), ticket['id']))
                db.execute('INSERT INTO cards(game_id,player_id,numbers) VALUES(?,?,?)',
                           (game['id'], pid, json.dumps(make_card())))
                self.log(db, 'card_claimed', {'player_id': pid}, game['id'])
            else:
                pid = player['id']
                # A code recovers the same identity and card; it never enrolls another player.
                self.log(db, 'card_recovered', {'player_id': pid}, game['id'])
            return self.new_session(db, 'player', pid)

    def registration(self, opened, expected_game):
        if type(opened) is not bool:
            raise Problem('領卡開關必須為布林值。')
        with self.transaction() as db:
            game = self.current(db)
            if game['id'] != expected_game:
                raise Problem('局次已更新，請重新整理。', 409)
            if opened and game['status'] != 'waiting':
                raise Problem('已開始開獎，無法再開放新領卡。', 409)
            db.execute('UPDATE games SET registration_open=? WHERE id=?', (int(opened), game['id']))
            self.log(db, 'registration', {'open': opened}, game['id'])

    def draw(self, expected_game, expected_round):
        with self.transaction() as db:
            game = self.current(db)
            previous = db.execute('SELECT number FROM draws WHERE game_id=? ORDER BY round', (game['id'],)).fetchall()
            if game['id'] != expected_game or len(previous) != expected_round:
                raise Problem('開獎進度已更新，請確認畫面後再抽號。', 409)
            if game['status'] == 'finished':
                raise Problem('本局已結束。', 409)
            if not db.execute('SELECT 1 FROM cards WHERE game_id=?', (game['id'],)).fetchone():
                raise Problem('尚無玩家領卡。', 409)
            drawn = {r['number'] for r in previous}
            remaining = sorted(set(range(1, 50)) - drawn)
            if not remaining:
                raise Problem('所有號碼皆已開出。', 409)
            number = secrets.choice(remaining)
            drawn.add(number)
            round_no = len(previous) + 1
            candidates = []
            cards = db.execute('SELECT * FROM cards WHERE game_id=? AND qualified_round IS NULL ORDER BY player_id',
                               (game['id'],)).fetchall()
            for card in cards:
                if completed_lines(json.loads(card['numbers']), drawn):
                    candidates.append(card['player_id'])
                    db.execute('UPDATE cards SET qualified_round=? WHERE id=?', (round_no, card['id']))
            count = db.execute('SELECT count(*) FROM winners WHERE game_id=?', (game['id'],)).fetchone()[0]
            order = candidates[:]
            secrets.SystemRandom().shuffle(order)
            selected = order[:max(0, game['max_winners'] - count)]
            for i, pid in enumerate(selected):
                db.execute('INSERT INTO winners(game_id,player_id,round,position) VALUES(?,?,?,?)',
                           (game['id'], pid, round_no, count + i + 1))
            db.execute('INSERT INTO draws VALUES(?,?,?,?,?,?,?)',
                       (game['id'], round_no, number, time.time(), json.dumps(candidates),
                        json.dumps(order), json.dumps(selected)))
            ended = count + len(selected) >= game['max_winners'] or round_no == 49
            db.execute('UPDATE games SET registration_open=0,status=?,ended_at=? WHERE id=?',
                       ('finished' if ended else 'playing', time.time() if ended else None, game['id']))
            result = {'round': round_no, 'number': number, 'candidates': candidates,
                      'lottery_order': order, 'selected': selected}
            self.log(db, 'draw', result, game['id'])
            return result

    def marks(self, player_id, game_id, indices):
        if not isinstance(indices, list) or len(indices) > 25 or any(type(i) is not int or not 0 <= i < 25 for i in indices):
            raise Problem('勾選格子資料無效。')
        with self.transaction(notify=False) as db:
            if self.current(db)['id'] != game_id:
                raise Problem('已進入下一局，請重新整理卡片。', 409)
            db.execute('UPDATE cards SET marks=? WHERE game_id=? AND player_id=?',
                       (json.dumps(sorted(set(indices) | {12})), game_id, player_id))

    def claimed(self, game_id, player_id, claimed):
        if type(claimed) is not bool:
            raise Problem('領獎狀態資料無效。')
        with self.transaction() as db:
            cursor = db.execute('UPDATE winners SET claimed_at=? WHERE game_id=? AND player_id=?',
                                (time.time() if claimed else None, game_id, player_id))
            if not cursor.rowcount:
                raise Problem('找不到得獎者。', 404)
            self.log(db, 'prize_claimed' if claimed else 'prize_unclaimed', {'player_id': player_id}, game_id)

    def reset(self, expected_game, confirmation):
        if confirmation != '開啟下一局':
            raise Problem('請輸入「開啟下一局」確認。')
        with self.transaction() as db:
            old = self.current(db)
            if old['id'] != expected_game:
                raise Problem('下一局已建立，請重新整理。', 409)
            db.execute("UPDATE games SET registration_open=0,status='finished',ended_at=COALESCE(ended_at,?) WHERE id=?",
                       (time.time(), old['id']))
            gid = db.execute('INSERT INTO games(title,created_at) VALUES(?,?)', (old['title'], time.time())).lastrowid
            for player in db.execute('SELECT id FROM players').fetchall():
                db.execute('INSERT INTO cards(game_id,player_id,numbers) VALUES(?,?,?)',
                           (gid, player['id'], json.dumps(make_card())))
            self.log(db, 'next_game', {'previous_game': old['id']}, gid)
            return gid

    def snapshot(self, player_id=None, admin=False, game_id=None):
        # Explicit read transaction keeps the full snapshot on the same committed revision.
        with self.connect() as db:
            db.execute('BEGIN')
            game = db.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone() if game_id else self.current(db)
            if not game:
                raise Problem('找不到這一局。', 404)
            gid = game['id']
            draws = [dict(r) for r in db.execute('SELECT round,number,drawn_at FROM draws WHERE game_id=? ORDER BY round', (gid,))]
            winners = [dict(r) for r in db.execute('SELECT w.*,p.name FROM winners w JOIN players p ON p.id=w.player_id WHERE game_id=? ORDER BY position', (gid,))]
            for w in winners:
                w['card_label'] = 'P%03d' % w['player_id']
            data = {'game': dict(game), 'draws': draws, 'winners': winners,
                    'player_count': db.execute('SELECT count(*) FROM cards WHERE game_id=?', (gid,)).fetchone()[0],
                    'revision': int(db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0]),
                    'demo': db.execute("SELECT value FROM settings WHERE key='demo'").fetchone()[0] == '1'}
            if player_id:
                card = db.execute('SELECT c.*,p.name FROM cards c JOIN players p ON p.id=c.player_id WHERE game_id=? AND player_id=?', (gid, player_id)).fetchone()
                if card:
                    data['card'] = dict(card)
                    data['card']['numbers'] = json.loads(card['numbers'])
                    data['card']['marks'] = json.loads(card['marks'])
                    data['card']['lines'] = completed_lines(data['card']['numbers'], {d['number'] for d in draws})
                    data['card']['card_label'] = 'P%03d' % player_id
            if admin:
                data['tickets'] = [dict(r) for r in db.execute('SELECT t.*,p.name,p.id AS player_id FROM tickets t LEFT JOIN players p ON p.ticket_id=t.id ORDER BY t.id')]
                data['games'] = [dict(r) for r in db.execute('SELECT * FROM games ORDER BY id DESC')]
                data['round_results'] = [{**dict(r), **{k: json.loads(r[k]) for k in ('candidates', 'lottery_order', 'selected')}}
                                         for r in db.execute('SELECT * FROM draws WHERE game_id=? ORDER BY round', (gid,))]
                data['players'] = [dict(r) for r in db.execute('SELECT p.id,p.name,c.qualified_round FROM players p JOIN cards c ON c.player_id=p.id WHERE c.game_id=? ORDER BY p.id', (gid,))]
            return data

    def audit_export(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id')]

    def export_all(self):
        with self.connect() as db:
            db.execute('BEGIN')
            result = {'format_version': 1, 'exported_at': time.time()}
            # Fixed table names, never user input. Do not export passwords or sessions.
            for table in ('games', 'tickets', 'players', 'cards', 'draws', 'winners', 'audit'):
                result[table] = [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
            return result

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == Path(self.path).resolve():
            raise Problem('備份路徑不可與資料庫相同。')
        with self.connect() as src, sqlite3.connect(str(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Problem('備份完整性檢查失敗。', 500)
