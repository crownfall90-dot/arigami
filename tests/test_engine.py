import copy
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from bot.engine import analyze, manage_position
from bot.report import render
from bot.server import Handler


def fixture():
    """Synthetic market ONLY. Never represents actual BTC/ETH quotes."""
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    candles = []
    for i in range(50):
        candles.append(dict(open=103, high=105, low=101, close=103, volume=100,
                            close_time=(now-timedelta(minutes=15*(49-i))).isoformat(), closed=True))
    candles[44]['low'] = 100
    candles[-1].update(open=101.5, high=102, low=99, close=101, volume=151)
    return dict(symbol='SYNTHETIC_TEST', exchange='TEST_ONLY', source='unit-test',
                timeframe_minutes=15, as_of=now.isoformat(), candles=candles,
                profile=dict(type='session', source='synthetic', start=candles[0]['close_time'],
                             end=candles[-1]['close_time'], poc=[100.95, 101.05], previous_poc=[100.95, 101.05],
                             val=[98, 99], vah=[145, 146], hvn=[[100.95, 101.05], [120, 121]], lvn=[[140, 141]]),
                vwap=dict(value=100.99, source='synthetic', as_of=candles[-1]['close_time']),
                smart_money=[dict(kind='OB', direction='LONG', zone=[100.95, 101.05], confirmed=True,
                                  unmitigated=True, source='synthetic', created_at=candles[-2]['close_time'])],
                cvd=dict(source='synthetic', method='aggressor_trades',
                         values=[dict(close_time=c['close_time'], value=10 if i == 49 else 0) for i, c in enumerate(candles)]),
                policy=dict(min_atr_ratio=.5),
                news=dict(verified=True, source='synthetic', checked_at=now.isoformat(),
                          coverage_start=now.isoformat(), coverage_end=(now+timedelta(hours=1)).isoformat(), events=[]),
                account=dict(deposit=10000, contract_type='linear', deposit_currency='USDT', quote_currency='USDT',
                             contract_multiplier=1, quantity_step=.001, taker_fee_bps=5, slippage_bps=5),
                position=False)


class EngineTests(unittest.TestCase):
    def test_approved_long_and_risk(self):
        result = analyze(fixture())
        self.assertEqual(result['decision'], 'LONG', result)
        self.assertEqual(len(result['checks']), 7)
        self.assertTrue(all(x['passed'] for x in result['checks']))
        self.assertLessEqual(result['trade']['estimated_risk'], 100)
        self.assertAlmostEqual(result['trade']['stop_loss'], 99-result['context']['atr'])

    def test_approved_short(self):
        d = fixture()
        for c in d['candles']:
            c.update(open=250-c['open'], close=250-c['close'], high=250-c['low'], low=250-c['high'])
        p = d['profile']
        flip = lambda z: [250-z[1], 250-z[0]]
        p['poc'], p['previous_poc'] = flip(p['poc']), flip(p['previous_poc'])
        p['val'], p['vah'] = flip(p['vah']), flip(p['val'])
        p['hvn'], p['lvn'] = [flip(z) for z in p['hvn']], [flip(z) for z in p['lvn']]
        d['vwap']['value'] = 250-d['vwap']['value']
        d['smart_money'][0].update(direction='SHORT', zone=flip(d['smart_money'][0]['zone']))
        for v in d['cvd']['values']:
            v['value'] = -v['value']
        self.assertEqual(analyze(d)['decision'], 'SHORT')

    def test_each_confirmation_blocks(self):
        mutations = [lambda d: d['profile'].update(poc=[102, 103], hvn=[[120, 121]]),
                     lambda d: d['vwap'].update(value=100),
                     lambda d: d.update(smart_money=[]),
                     lambda d: d['candles'][-1].update(low=100),
                     lambda d: d['cvd']['values'][-1].update(value=-1),
                     lambda d: d['candles'][-1].update(volume=150),
                     lambda d: d['profile'].update(hvn=[[100.95, 101.05], [102, 103]], lvn=[[104, 105]])]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                d = fixture()
                mutate(d)
                r = analyze(d)
                self.assertEqual(r['decision'], 'ПРОПУСК')
                self.assertFalse(r['checks'][index]['passed'])
                self.assertIsNone(r['trade'])

    def test_missing_required_inputs(self):
        for key in ('cvd', 'profile', 'vwap', 'smart_money', 'news', 'policy', 'candles'):
            with self.subTest(key=key):
                d = fixture()
                del d[key]
                r = analyze(d)
                self.assertEqual(r['decision'], 'ПРОПУСК')
                self.assertTrue(r['missing'])

    def test_volume_uses_previous_twenty(self):
        r = analyze(fixture())
        self.assertEqual(r['context']['volume']['avg20'], 100)
        self.assertEqual(r['context']['volume']['ratio'], 1.51)

    def test_news_boundary(self):
        for minutes, expected in [(14, 'ПРОПУСК'), (15, 'LONG'), (0, 'ПРОПУСК')]:
            d = fixture()
            d['news']['events'] = [dict(time=(datetime.fromisoformat(d['as_of'])+timedelta(minutes=minutes)).isoformat(), relevant=True, impact='high')]
            self.assertEqual(analyze(d)['decision'], expected)

    def test_stale_or_open_candle_blocks(self):
        d = fixture()
        d['candles'][-1]['closed'] = False
        self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')
        d = fixture()
        d['as_of'] = (datetime.fromisoformat(d['as_of'])+timedelta(seconds=61)).isoformat()
        self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')

    def test_cvd_timestamp_or_proxy_blocks(self):
        d = fixture()
        d['cvd']['method'] = 'ohlcv_proxy'
        self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')
        d = fixture()
        d['cvd']['values'][-1]['close_time'] = d['cvd']['values'][-2]['close_time']
        self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')

    def test_mitigated_zone_blocks(self):
        d = fixture()
        d['smart_money'][0]['created_at'] = d['candles'][-5]['close_time']
        self.assertFalse(analyze(d)['checks'][2]['passed'])

    def test_no_deposit_does_not_invent_size(self):
        d = fixture()
        del d['account']
        r = analyze(d)
        self.assertEqual(r['decision'], 'LONG')
        self.assertIsNone(r['trade']['size'])

    def test_invalid_inputs_fail_closed(self):
        for d in (None, [], {}, {'symbol': True}, {'candles': 'bad'}):
            self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')
        for value in (True, float('nan'), float('inf'), '101'):
            d = fixture()
            d['candles'][-1]['close'] = value
            self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')

    def test_unknown_position_is_not_flat(self):
        d = fixture()
        del d['position']
        self.assertIn('НЕТ ДАННЫХ', analyze(d)['position']['status'])

    def test_position_blocks_additional_entry_and_trailing_never_widens(self):
        d = fixture()
        d['position'] = dict(direction='LONG', entry=101, stop_loss=95, initial_stop_loss=95,
                             tp1=120, tp2=145, quantity=2, contract_multiplier=1, tp1_filled=True,
                             lvn_entry_confirmed=True)
        self.assertEqual(analyze(d)['decision'], 'ПРОПУСК')
        c = dict(price=140.5, poc=[130, 131], lvn=[[140, 141]])
        p = manage_position(d['position'], c)
        self.assertEqual(p['action'], 'ПОДТЯНУТЬ СТОП')
        self.assertEqual(p['stop_loss'], 130)
        c['poc'] = [90, 91]
        self.assertEqual(manage_position(d['position'], c)['stop_loss'], 95)

    def test_report_has_four_sections(self):
        report = render(analyze(fixture()))
        for label in ('1. АНАЛИЗ', '2. ЧЕК-ЛИСТ', '3. РЕШЕНИЕ', '4. УПРАВЛЕНИЕ ПОЗИЦИЕЙ'):
            self.assertIn(label, report)

    def test_cli_outputs_utf8_under_windows_legacy_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING='cp1251')
        run = subprocess.run([sys.executable, '-m', 'bot', 'examples/synthetic-long.json', '--json'],
                             capture_output=True, env=env, check=True)
        self.assertEqual(json.loads(run.stdout.decode('utf-8'))['decision'], 'LONG')


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_health_and_ui(self):
        with urlopen(self.url+'/health') as r:
            self.assertFalse(json.load(r)['execution'])
        with urlopen(self.url) as r:
            self.assertIn('Тройное подтверждение', r.read().decode())

    def test_missing_data_skip(self):
        req = Request(self.url+'/api/analyze', data=b'{}', headers={'Content-Type': 'application/json'})
        with urlopen(req) as r:
            self.assertEqual(json.load(r)['analysis']['decision'], 'ПРОПУСК')

    def test_historical_data_rejected_by_live_api(self):
        req = Request(self.url+'/api/analyze', data=json.dumps(fixture()).encode(), headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as err:
            urlopen(req)
        self.assertEqual(err.exception.code, 400)
        err.exception.close()

    def test_cross_origin_rejected(self):
        req = Request(self.url+'/api/analyze', data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'https://example.com'})
        with self.assertRaises(HTTPError) as err:
            urlopen(req)
        self.assertEqual(err.exception.code, 403)
        err.exception.close()
