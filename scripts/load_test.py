"""Isolated 150-player HTTP/SSE rehearsal. Never touches the event database."""
import argparse
import concurrent.futures
import http.client
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bingo.game import Store
from bingo.server import BingoServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--players', type=int, default=150)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--output', default='docs/load-test.json')
    args = parser.parse_args()
    if not 1 <= args.players <= 200 or not 1 <= args.rounds <= 49: parser.error('players 1..200, rounds 1..49')
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / 'load.sqlite3'); store.initialize('load-test-password')
        store.create_tickets(args.players); store.registration(True, 1)
        codes = [t['code'] for t in store.snapshot(admin=True)['tickets']]
        server = BingoServer(('127.0.0.1', 0), store)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
        latencies, streams = [], []
        port = server.server_port

        def request(path, body=None, cookie=None, csrf=None):
            start = time.perf_counter()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            headers = {'Content-Type': 'application/json'}
            if cookie: headers['Cookie'] = cookie
            if csrf: headers['X-CSRF-Token'] = csrf
            conn.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse(); result = json.loads(response.read())
            cookie = response.getheader('Set-Cookie', '').split(';')[0]
            assert response.status == 200, (response.status, result)
            conn.close(); latencies.append((time.perf_counter() - start) * 1000)
            return result, cookie

        def claim(i):
            _, cookie = request('/api/claim', {'name': '測試來賓%03d' % i, 'code': codes[i]})
            state, _ = request('/api/state', cookie=cookie)
            return {'cookie': cookie, 'csrf': state['csrf'], 'numbers': state['card']['numbers'], 'id': state['card']['id']}

        def stream(_):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            conn.request('GET', '/api/events'); response = conn.getresponse()
            assert response.status == 200
            while response.readline().strip(): pass
            return conn, response

        started = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.players) as pool:
                players = list(pool.map(claim, range(args.players)))
                streams = list(pool.map(stream, range(args.players)))
                assert store.snapshot()['player_count'] == args.players
                print('%d players claimed, %d SSE connections open' % (len(players), len(streams)), flush=True)
                _, admin_cookie = request('/api/login', {'password': 'load-test-password'})
                host, _ = request('/api/admin/state', cookie=admin_cookie)
                csrf = host['csrf']
                propagation = []
                actual_rounds = 0
                for round_no in range(args.rounds):
                    if store.snapshot()['game']['status'] == 'finished': break
                    draw_started = time.perf_counter()
                    request('/api/admin/draw', {'game_id': 1, 'expected_round': round_no}, admin_cookie, csrf)
                    def receive(item):
                        _, response = item
                        while True:
                            line = response.readline()
                            if line.startswith(b'data:'): return json.loads(line[5:])['revision']
                            assert line, 'SSE connection closed'
                    revisions = list(pool.map(receive, streams))
                    assert all(r == store.revision for r in revisions)
                    propagation.append((time.perf_counter() - draw_started) * 1000)
                    def sync(player):
                        state, _ = request('/api/state', cookie=player['cookie'])
                        assert len(state['draws']) == round_no + 1
                        assert state['card']['numbers'] == player['numbers']
                        request('/api/marks', {'game_id': 1, 'marks': [0,12,24]}, player['cookie'], player['csrf'])
                    list(pool.map(sync, players)); actual_rounds += 1
                # Close and reopen all streams and recover every card with its original code.
                for conn, response in streams: response.close(); conn.close()
                streams = list(pool.map(stream, range(args.players)))
                recovered = list(pool.map(claim, range(args.players)))
                assert all(p['id'] == r['id'] and p['numbers'] == r['numbers'] for p,r in zip(players,recovered))
                for p in recovered:
                    state, _ = request('/api/state', cookie=p['cookie'])
                    assert state['card']['marks'] == [0,12,24]
                store.backup(Path(temp) / 'backup.sqlite3')
                assert Store(Path(temp) / 'backup.sqlite3').snapshot()['draws'] == store.snapshot()['draws']
                sorted_ms = sorted(latencies)
                report = {
                    'passed': True, 'platform': platform.platform(), 'python': platform.python_version(),
                    'players': args.players, 'simultaneous_sse': len(streams), 'rounds': actual_rounds,
                    'http_requests': len(latencies), 'http_ms_median': round(statistics.median(latencies), 2),
                    'http_ms_p95': round(sorted_ms[int(.95 * (len(sorted_ms)-1))], 2),
                    'sse_all_clients_ms_max': round(max(propagation), 2),
                    'elapsed_seconds': round(time.perf_counter()-started, 2),
                    'checks': ['concurrent claim', '150 live SSE streams', 'draw broadcast', 'concurrent snapshot + marks',
                               'SSE reconnect', 'original code recovery', 'marks persisted', 'live backup integrity'],
                    'scope': 'Loopback on this machine; repeat on the actual ARM VPS and venue network.'
                }
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
                print(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            for conn, response in streams:
                response.close(); conn.close()
            server.shutdown(); server.server_close(); server_thread.join()


if __name__ == '__main__': main()
