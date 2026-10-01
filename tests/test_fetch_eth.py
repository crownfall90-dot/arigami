from unittest import TestCase
from unittest.mock import patch
from bot.fetch_eth import fetch


class FetchTests(TestCase):
    def rows(self):
        start = 1767225600000
        return [[start+i*900000, '2000', '2001', '1999', '2000', '100',
                 start+(i+1)*900000-1, '200000', 10, '75', '150000', '0'] for i in range(101)]

    def test_real_aggressor_fields_and_unclosed_candle(self):
        rows = self.rows()
        now = rows[-1][0]+1000
        with patch('bot.fetch_eth.get', side_effect=[{'serverTime': now}, rows]):
            data, raw = fetch()
        self.assertEqual(data['symbol'], 'ETHUSDT')
        self.assertEqual(len(data['candles']), 100)
        self.assertEqual(data['cvd']['values'][-1]['value'], 5000)
        self.assertEqual(data['vwap']['value'], 2000)
        self.assertNotIn('profile', data)
        self.assertNotIn('account', data)
        self.assertNotIn('position', data)

    def test_invalid_taker_volume_rejected(self):
        rows = self.rows()
        rows[0][9] = '101'
        with patch('bot.fetch_eth.get', side_effect=[{'serverTime': rows[-1][6]+1}, rows]):
            with self.assertRaises(ValueError):
                fetch()
