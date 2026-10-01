"""Download public ETHUSDT perpetual candles; never fabricate missing evidence."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

BASE = 'https://api.binance.com'


def get(path, **params):
    url = BASE + path + ('?' + urlencode(params) if params else '')
    with urlopen(url, timeout=25) as response:
        return json.load(response)


def bybit_get(path, **params):
    url = 'https://api.bybit.com' + path + '?' + urlencode(params)
    with urlopen(url, timeout=25) as response:
        body = json.load(response)
    if body.get('retCode') != 0:
        raise ValueError(f"Bybit: {body.get('retMsg', 'unknown error')}")
    return body


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def fetch():
    try:
        server_time = get('/api/v3/time')['serverTime']
        raw = get('/api/v3/klines', symbol='ETHUSDT', interval='15m', limit=200,
                  endTime=server_time)
    except HTTPError as exc:
        if exc.code != 451:
            raise
        return fetch_bybit()
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
    source = BASE + '/api/v3/klines?symbol=ETHUSDT&interval=15m'
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
    data = dict(symbol='ETHUSDT', exchange='Binance Spot',
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
    attach_local_evidence(data)
    return data, raw


def fetch_bybit():
    """Candle-only fallback when Binance blocks this network with HTTP 451.

    It intentionally omits CVD and the trade-built profile, so the engine emits
    ПРОПУСК rather than treating candle volume as order-flow evidence.
    """
    body = bybit_get('/v5/market/kline', category='spot', symbol='ETHUSDT', interval='15', limit=200)
    server_time = int(body['time'])
    raw = list(reversed(body['result']['list']))
    closed = [row for row in raw if int(row[0]) + 900_000 <= server_time]
    if len(closed) < 35:
        raise ValueError('Bybit returned fewer than 35 closed candles')
    candles = [dict(open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                    volume=float(row[5]), close_time=iso(int(row[0])+899_999), closed=True) for row in closed]
    session_start = int(closed[-1][0]) // 86_400_000 * 86_400_000
    session = [row for row in closed if int(row[0]) >= session_start]
    total_base = sum(Decimal(row[5]) for row in session)
    total_quote = sum(Decimal(row[6]) for row in session)
    if total_base <= 0:
        raise ValueError('Zero Bybit session volume')
    source = 'https://api.bybit.com/v5/market/kline?category=spot&symbol=ETHUSDT&interval=15'
    data = dict(symbol='ETHUSDT', exchange='Bybit Spot (fallback)', source=source,
                timeframe_minutes=15, as_of=iso(server_time), candles=candles,
                vwap=dict(value=float(total_quote/total_base), source=source,
                          as_of=candles[-1]['close_time'], session_start=iso(session_start),
                          method='sum(turnover) / sum(volume), UTC session'),
                provenance=dict(retrieved_at=datetime.now(timezone.utc).isoformat(), exchange_time=iso(server_time),
                                raw_file='ethusdt-15m-raw.json',
                                missing=['CVD: Binance trade source unavailable',
                                         'Volume Profile: Binance trade source unavailable',
                                         'OB/FVG: not verified', 'Economic calendar: not verified',
                                         'ATR low-volatility threshold: not configured',
                                         'Deposit and open position: not provided']))
    data['policy'] = {'min_atr_ratio': .5, 'source': 'local default; current ATR must be >= 50% of average previous 20 ATR'}
    data['provenance']['missing'].remove('ATR low-volatility threshold: not configured')
    return data, raw


def attach_local_evidence(data):
    """Use only continuous local collector evidence; otherwise preserve missing fields."""
    try:
        from .live import profile as local_profile
        closing = int(datetime.fromisoformat(data['candles'][-1]['close_time']).timestamp()*1000)
        current_profile = local_profile(None, closing)
    except Exception as exc:
        data['provenance']['missing'].append(f'Live Volume Profile unavailable: {exc}')
        return
    if current_profile:
        data['profile'] = current_profile
        data['provenance']['missing'].remove('Volume Profile: requires volume-at-price data')
    zones=[]
    candles=data['candles']
    for direction in ('LONG','SHORT'):
        for i in range(2,len(candles)):
            gap = candles[i-2]['high'] < candles[i]['low'] if direction == 'LONG' else candles[i]['high'] < candles[i-2]['low']
            if not gap: continue
            fvg=sorted((candles[i-2]['high'],candles[i]['low'])) if direction=='LONG' else sorted((candles[i]['high'],candles[i-2]['low']))
            if not any(c['low']<=fvg[1] and c['high']>=fvg[0] for c in candles[i+1:-1]):
                zones.append(dict(kind='FVG',direction=direction,zone=fvg,confirmed=True,unmitigated=True,
                                  source='mechanical 3-bar imbalance from Binance 15m candles',created_at=candles[i]['close_time']))
            opposite = (lambda c:c['close']<c['open']) if direction=='LONG' else (lambda c:c['close']>c['open'])
            source=next((j for j in range(i-1,max(-1,i-6),-1) if opposite(candles[j])),None)
            if source is not None:
                ob=sorted((candles[source]['open'],candles[source]['close']))
                if ob[0]!=ob[1] and not any(c['low']<=ob[1] and c['high']>=ob[0] for c in candles[i+1:-1]):
                    zones.append(dict(kind='OB',direction=direction,zone=ob,confirmed=True,unmitigated=True,
                                      source='last opposite candle body before mechanical 3-bar imbalance',created_at=candles[i]['close_time']))
    if zones:
        data['smart_money']=zones
        data['provenance']['missing'].remove('OB/FVG: not verified')
    data['policy']={'min_atr_ratio':.5, 'source':'local default; current ATR must be >= 50% of average previous 20 ATR'}
    data['provenance']['missing'].remove('ATR low-volatility threshold: not configured')


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
