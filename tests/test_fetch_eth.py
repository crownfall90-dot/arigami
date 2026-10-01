from unittest import TestCase
from unittest.mock import patch
from bot.fetch_eth import fetch, fetch_bybit


class FetchTests(TestCase):
    def rows(self):
        start = 1767225600000
        return [[start+i*900000, '2000', '2001', '1999', '2000', '100',
                 start+(i+1)*900000-1, '200000', 10, '75', '150000', '0'] for i in range(101)]

    def test_real_aggressor_fields_and_unclosed_candle(self):
        rows = self.rows()
        now = rows[-1][0]+1000
        with patch('bot.fetch_eth.get', side_effect=[{'serverTime': now}, rows]), \
             patch('bot.fetch_eth.attach_local_evidence'):
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
        with patch('bot.fetch_eth.get', side_effect=[{'serverTime': rows[-1][6]+1}, rows]), \
             patch('bot.fetch_eth.attach_local_evidence'):
            with self.assertRaises(ValueError):
                fetch()

    def test_bybit_fallback_never_invents_cvd(self):
        rows = [[str(1767225600000+i*900000), '2000', '2001', '1999', '2000', '100', '200000'] for i in range(100)]
        with patch('bot.fetch_eth.bybit_get', return_value={'time': 1767315600000, 'result': {'list': list(reversed(rows))}}):
            data, _ = fetch_bybit()
        self.assertEqual(data['exchange'], 'Bybit Spot (fallback)')
        self.assertNotIn('cvd', data)
        self.assertIn('CVD: Binance trade source unavailable', data['provenance']['missing'])
