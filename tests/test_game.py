import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bingo.game import LINES, Problem, Store, completed_lines, make_card


class GameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.store.initialize('test-password-123')
        self.store.create_tickets(20)
        self.store.registration(True, 1)
        self.codes = [t['code'] for t in self.store.snapshot(admin=True)['tickets']]

    def tearDown(self):
        self.tmp.cleanup()

    def enroll(self, count):
        for i in range(count):
            self.store.claim(self.codes[i], '來賓' + str(i))

    def arranged_card(self, pid, line):
        remaining = [n for n in range(7, 50) if n not in line][:19]
        values = line + remaining
        values.insert(12, 0)
        with self.store.connect() as db:
            db.execute('UPDATE cards SET numbers=? WHERE game_id=1 AND player_id=?', (json.dumps(values), pid))

    def draw_number(self, n):
        with patch('bingo.game.secrets.choice', return_value=n):
            self.store.draw(1, len(self.store.snapshot()['draws']))

    def test_random_card_and_all_twelve_lines(self):
        for _ in range(200):
            card = make_card()
            self.assertEqual(len(card), 25)
            self.assertEqual(card[12], 0)
            self.assertEqual(len(set(card) - {0}), 24)
            self.assertTrue(all(1 <= n <= 49 for n in card if n))
        card = list(range(1, 26)); card[12] = 0
        for line in LINES:
            drawn = {card[i] for i in line if card[i]}
            self.assertIn(line, completed_lines(card, drawn))
            drawn.remove(next(iter(drawn)))
            self.assertNotIn(line, completed_lines(card, drawn))

    def test_same_ticket_claim_is_atomic_and_recovers_original(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            tokens = list(pool.map(lambda _: self.store.claim(self.codes[0], '同一人'), range(12)))
        ids = [self.store.session(t, 'player')['player_id'] for t in tokens]
        self.assertEqual(set(ids), {1})
        self.assertEqual(self.store.snapshot()['player_count'], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM players').fetchone()[0], 1)
        with self.assertRaises(Problem):
            self.store.claim(self.codes[1], '另一人', existing_player=1)

    def test_ticket_restores_after_registration_closes_and_ignores_new_name(self):
        self.enroll(1)
        original = self.store.snapshot(player_id=1)['card']
        self.draw_number(49)
        token = self.store.claim(self.codes[0], '改名不會生效')
        self.assertEqual(self.store.session(token, 'player')['player_id'], 1)
        self.assertEqual(self.store.snapshot(player_id=1)['card']['numbers'], original['numbers'])
        self.assertEqual(self.store.snapshot(player_id=1)['card']['name'], '來賓0')
        with self.assertRaises(Problem):
            self.store.claim(self.codes[1], '遲到玩家')
        with self.assertRaises(Problem):
            self.store.registration(True, 1)

    def test_simultaneous_overflow_persists_complete_lottery(self):
        self.enroll(12)
        for pid in range(1, 13): self.arranged_card(pid, [1, 2, 3, 4, 5])
        # Manual marks do not cause a win and no marks are required to win.
        self.store.marks(1, 1, list(range(25)))
        self.assertEqual(self.store.snapshot()['winners'], [])
        for n in range(1, 6): self.draw_number(n)
        snapshot = self.store.snapshot(admin=True)
        self.assertEqual(len(snapshot['winners']), 10)
        self.assertEqual(snapshot['game']['status'], 'finished')
        result = snapshot['round_results'][-1]
        self.assertEqual(set(result['candidates']), set(range(1, 13)))
        self.assertEqual(set(result['lottery_order']), set(range(1, 13)))
        self.assertEqual(result['selected'], result['lottery_order'][:10])
        self.assertTrue(all(p['qualified_round'] == 5 for p in snapshot['players']))
        self.assertTrue(all(w['round'] == 5 for w in snapshot['winners']))
        self.assertEqual(Store(self.path).snapshot(admin=True)['round_results'], snapshot['round_results'])
        with self.assertRaises(Problem): self.store.draw(1, 5)

    def test_earlier_round_has_priority_and_only_one_remaining_slot(self):
        self.enroll(15)
        for pid in range(1, 10): self.arranged_card(pid, [1, 2, 3, 4, 5])
        for pid in range(10, 16): self.arranged_card(pid, [1, 2, 3, 4, 6])
        for n in range(1, 7): self.draw_number(n)
        data = self.store.snapshot(admin=True)
        self.assertEqual([w['round'] for w in data['winners']], [5] * 9 + [6])
        self.assertEqual(len(data['round_results'][-1]['candidates']), 6)
        self.assertEqual(len(data['round_results'][-1]['selected']), 1)
        self.assertEqual(len({w['player_id'] for w in data['winners']}), 10)

    def test_racing_draw_commands_cannot_advance_two_rounds(self):
        self.enroll(2)
        def draw(_):
            try: return self.store.draw(1, 0)
            except Problem as e: return e.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(draw, range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn(409, results)
        self.assertEqual(len(self.store.snapshot()['draws']), 1)

    def test_restart_backup_claims_and_next_game_preserve_history(self):
        self.enroll(1); self.arranged_card(1, [1,2,3,4,5])
        self.store.marks(1, 1, [0, 3])
        token = self.store.login('test-password-123')
        for n in range(1, 6): self.draw_number(n)
        self.store.claimed(1, 1, True)
        before = self.store.snapshot(player_id=1, admin=True)
        restarted = Store(self.path)
        self.assertIsNotNone(restarted.session(token, 'admin'))
        self.assertEqual(restarted.snapshot(player_id=1, admin=True), before)
        destination = Path(self.tmp.name) / 'backup.sqlite3'
        restarted.backup(destination)
        self.assertEqual(Store(destination).snapshot(player_id=1, admin=True), before)
        gid = restarted.reset(1, '開啟下一局')
        new = restarted.snapshot(player_id=1)
        self.assertEqual(gid, 2)
        self.assertEqual(new['card']['marks'], [12])
        self.assertEqual(new['player_count'], 1)
        self.assertNotEqual(new['card']['numbers'], before['card']['numbers'])
        self.assertEqual(new['game']['registration_open'], 0)
        old = restarted.snapshot(admin=True, game_id=1)
        self.assertEqual(old['winners'], before['winners'])
        exported = restarted.export_all()
        self.assertEqual(len(exported['games']), 2)
        self.assertEqual(len(exported['cards']), 2)
        self.assertEqual(len(exported['draws']), 5)
        self.assertTrue(exported['winners'][0]['claimed_at'])
        self.assertNotIn('sessions', exported)
        self.assertNotIn('settings', exported)
        with self.assertRaises(Problem): restarted.reset(1, '開啟下一局')
        with self.assertRaises(Problem): restarted.marks(1, 1, [0])

    def test_all_draws_are_unique_and_stop_at_winners(self):
        self.enroll(2)
        while self.store.snapshot()['game']['status'] != 'finished':
            self.store.draw(1, len(self.store.snapshot()['draws']))
        data = self.store.snapshot()
        self.assertEqual(len(data['draws']), 49)  # Fewer than 10 players => exhaust all numbers.
        self.assertEqual(len({d['number'] for d in data['draws']}), 49)
        self.assertEqual(len(data['winners']), 2)

    def test_password_change_revokes_admin_session(self):
        token = self.store.login('test-password-123')
        self.store.change_password('new-password-123')
        self.assertIsNone(self.store.session(token, 'admin'))
        with self.assertRaises(Problem): self.store.login('test-password-123')
        self.assertIsNotNone(self.store.login('new-password-123'))


if __name__ == '__main__':
    unittest.main()
