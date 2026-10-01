"""Download public ETHUSDT perpetual candles; never fabricate missing evidence."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

BASE = 'https://fapi.binance.com'


def get(path, **params):
    url = BASE + path + ('?' + urlencode(params) if params else '')
    with urlopen(url, timeout=25) as response:
        return json.load(response)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def fetch():
    server_time = get('/fapi/v1/time')['serverTime']
    raw = get('/fapi/v1/klines', symbol='ETHUSDT', interval='15m', limit=200,
              endTime=server_time)
    closed = [row for row in raw if row[6] < server_time]
    if len(closed) < 35:
        raise ValueError('Binance returned fewer than 35 closed candles')
    candles, cvd, cumulative = [], [], Decimal(0)
    for row in closed:
        volume, buy = Decimal(row[5]), Decimal(row[9])
        if volume < 0 or not 0 <= buy <= volume:
            raise ValueError('Invalid aggressor volume from Binance')
        time = iso(row[6])
        candles.append(dict(open=float(row[1]), high=float(row[2]), low=float(row[3]),
                            close=float(row[4]), volume=float(volume), close_time=time, closed=True))
        cumulative += 2 * buy - volume
        cvd.append(dict(close_time=time, value=float(cumulative)))
    source = BASE + '/fapi/v1/klines?symbol=ETHUSDT&interval=15m'
    # Session is UTC midnight of the last closed candle's OPEN time.
    # At 00:00, the just-closed candle still belongs to the previous UTC session.
    session_start = closed[-1][0] // 86400000 * 86400000
    session = [r for r in closed if r[0] >= session_start]
    if session[0][0] != session_start:
        raise ValueError('Incomplete UTC session: VWAP cannot be calculated')
    total_base = sum(Decimal(r[5]) for r in session)
    total_quote = sum(Decimal(r[7]) for r in session)
    if total_base <= 0:
        raise ValueError('Zero session volume')
    data = dict(symbol='ETHUSDT', exchange='Binance USD-M perpetual futures',
                source=source, timeframe_minutes=15, as_of=iso(server_time), candles=candles,
                vwap=dict(value=float(total_quote/total_base), source=source,
                          as_of=candles[-1]['close_time'], session_start=iso(session_start),
                          method='sum(quote_asset_volume) / sum(base_asset_volume), UTC session'),
                cvd=dict(source=source, method='aggressor_trades', values=cvd,
                         calculation='cumulative sum(2 * taker_buy_base_asset_volume - base_asset_volume)',
                         anchor=candles[0]['close_time'], unit='ETH'),
                provenance=dict(retrieved_at=datetime.now(timezone.utc).isoformat(),
                                exchange_time=iso(server_time), raw_file='ethusdt-15m-raw.json',
                                missing=['Volume Profile: requires volume-at-price data',
                                         'OB/FVG: not verified', 'Economic calendar: not verified',
                                         'ATR low-volatility threshold: not configured',
                                         'Deposit and open position: not provided']))
    return data, raw


def save_snapshot(data, raw):
    """Persist successful pulls only; a failed refresh never overwrites the last snapshot."""
    folder = Path(__file__).resolve().parents[1] / 'private-data'
    folder.mkdir(exist_ok=True)
    for filename, value in [('ethusdt-15m.json', data), ('ethusdt-15m-raw.json', raw)]:
        path = folder / filename
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def main():
    data, raw = fetch()
    save_snapshot(data, raw)
    folder = Path(__file__).resolve().parents[1] / 'private-data'
    print(json.dumps(dict(path=str(folder / 'ethusdt-15m.json'), symbol=data['symbol'],
                         candles=len(data['candles']), as_of=data['as_of'],
                         last_closed=data['candles'][-1]['close_time'],
                         close=data['candles'][-1]['close'], vwap=data['vwap']['value'])))


if __name__ == '__main__':
    main()
