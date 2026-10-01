import tempfile
import unittest

from bot.live import connect, profile, session_start_ms, save, state, validate


class LiveCollectorTests(unittest.TestCase):
    def event(self, aggregate_id, when, price, quantity='1'):
        return {'a': aggregate_id, 'p': str(price), 'q': quantity, 'T': when,
                'm': False, 's': 'ETHUSDT'}

    def test_rest_payload_without_symbol_is_valid(self):
        event = self.event(1, 1_000, 3000)
        del event['s']
        self.assertEqual(validate(event)[0], 1)

    def test_wrong_symbol_is_rejected(self):
        event = self.event(1, 1_000, 3000)
        event['s'] = 'BTCUSDT'
        with self.assertRaises(ValueError):
            validate(event)

    def test_profile_requires_session_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect(__import__('pathlib').Path(folder) / 'trades.sqlite3')
            end = 1_800_000_000_000
            start = session_start_ms(end)
            state(db, 'bootstrap_session_start', start)
            state(db, 'bootstrap_complete', end)
            save(db, self.event(1, start + 500, 3000, '5'))
            save(db, self.event(2, end - 500, 3001, '2'))
            db.commit()
            result = profile(db, end)
            self.assertIsNotNone(result)
            self.assertEqual(result['type'], 'session')
            self.assertIn('poc', result)
            db.close()
