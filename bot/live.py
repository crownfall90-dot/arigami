"""Local ETHUSDT spot-trade collector; market data only, no API keys or orders."""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import websockets

SYMBOL = 'ETHUSDT'
REST = 'https://data-api.binance.vision'
STREAM = 'wss://stream.binance.com:9443/ws/ethusdt@aggTrade'
ROOT = Path(__file__).resolve().parents[1] / 'private-data'
DATABASE = ROOT / 'ethusdt-live.sqlite3'
BIN_SIZE = 1.0


def now_ms(): return int(time.time() * 1000)
def session_start_ms(value=None): return (value or now_ms()) // 86_400_000 * 86_400_000
def iso(value): return datetime.fromtimestamp(value/1000, timezone.utc).isoformat()


def connect(path=DATABASE):
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('''CREATE TABLE IF NOT EXISTS trades (
        aggregate_id INTEGER PRIMARY KEY, event_time INTEGER NOT NULL, price REAL NOT NULL,
        quantity REAL NOT NULL, buyer_is_maker INTEGER NOT NULL CHECK(buyer_is_maker IN (0,1))
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS trades_time ON trades(event_time)')
    db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return db


def state(db, key, value=None):
    if value is not None:
        db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
        db.commit(); return value
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def validate(event):
    # REST /aggTrades omits ``s`` while the WebSocket payload includes it.
    # Both endpoints are scoped to ETHUSDT by the request URL, so accepting the
    # absent REST field is safe; a supplied symbol must still match exactly.
    required = ('a', 'p', 'q', 'T', 'm')
    if (not isinstance(event, dict) or any(k not in event for k in required)
            or event.get('s', SYMBOL) != SYMBOL):
        raise ValueError('Unexpected aggregate trade payload')
    aid, price, quantity, when = int(event['a']), float(event['p']), float(event['q']), int(event['T'])
    if aid < 0 or when <= 0 or not all(math.isfinite(x) and x > 0 for x in (price, quantity)) or not isinstance(event['m'], bool):
        raise ValueError('Invalid aggregate trade values')
    return aid, when, price, quantity, int(event['m'])


def save(db, event):
    db.execute('INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?)', validate(event))


def request(path, **params):
    url = REST + path + '?' + urlencode(params)
    with urlopen(url, timeout=35) as response:
        return json.load(response)


def bootstrap(db, maximum=600_000):
    """Fill current UTC session via the public spot REST history before streaming."""
    db.execute("DELETE FROM state WHERE key IN ('bootstrap_error','bootstrap_complete','bootstrap_imported')")
    db.commit()
    start, imported = session_start_ms(), 0
    state(db, 'bootstrap_session_start', start)
    row = db.execute('SELECT MAX(aggregate_id), MAX(event_time) FROM trades WHERE event_time>=?', (start,)).fetchone()
    from_id = int(row[0]) + 1 if row[0] is not None else None
    complete = False
    while imported < maximum:
        params = dict(symbol=SYMBOL, limit=min(1000, maximum-imported))
        if from_id is None: params['startTime'] = start
        else: params['fromId'] = from_id
        batch = request('/api/v3/aggTrades', **params)
        if not batch: break
        for event in batch:
            save(db, event); imported += 1
        db.commit()
        last = validate(batch[-1])
        from_id = last[0] + 1
        if last[1] >= now_ms()-1000:
            complete = True
            break
        # A short page before the present would make a silent hole in CVD and
        # the profile.  Do not classify that as a successful bootstrap.
        if len(batch) < params['limit']:
            raise ValueError('Aggregate-trade history ended before the present')
    if not complete:
        raise ValueError(f'Bootstrap limit {maximum} reached before the present')
    state(db, 'bootstrap_complete', int(now_ms()))
    state(db, 'bootstrap_imported', imported)
    return imported


def histogram(db, until):
    start = session_start_ms(until)
    rows = db.execute('SELECT CAST(price / ? AS INTEGER) AS bin, SUM(quantity) FROM trades WHERE event_time>=? AND event_time<=? GROUP BY bin', (BIN_SIZE,start,until)).fetchall()
    return {bin_id*BIN_SIZE: volume for bin_id, volume in rows if volume > 0}


def profile(db, close_time):
    owned = db is None
    db = db or connect()
    start = session_start_ms(close_time)
    if state(db, 'bootstrap_session_start') != str(start) or not state(db, 'bootstrap_complete'):
        if owned: db.close()
        return None
    hist = histogram(db, close_time)
    coverage = db.execute('SELECT MIN(event_time), MAX(event_time) FROM trades WHERE event_time>=? AND event_time<=?', (start,close_time)).fetchone()
    # No profile is returned unless every visible part of the session has data.
    if not hist or not coverage[0] or coverage[0] > start+1000 or coverage[1] < close_time-1000:
        if owned: db.close()
        return None
    poc = max(hist, key=lambda p: (hist[p], -p)); total = sum(hist.values()); running = hist[poc]; low=high=poc
    while running < total*.70:
        left, right = hist.get(low-BIN_SIZE,-1), hist.get(high+BIN_SIZE,-1)
        if left < 0 and right < 0: break
        if right > left: high += BIN_SIZE; running += max(right,0)
        else: low -= BIN_SIZE; running += max(left,0)
    threshold = hist[poc]*.10
    prices = sorted(hist)
    hvn = [p for p in prices if hist[p]>=threshold and hist[p]>=hist.get(p-BIN_SIZE,0) and hist[p]>=hist.get(p+BIN_SIZE,0)]
    lvn = [p for p in prices[1:-1] if hist[p]<=threshold*.35 and hist[p]<=hist.get(p-BIN_SIZE,math.inf) and hist[p]<=hist.get(p+BIN_SIZE,math.inf)]
    result = dict(type='session', source='Binance Spot aggTrade WebSocket + REST bootstrap', start=iso(start), end=iso(close_time),
                poc=[poc-BIN_SIZE,poc+BIN_SIZE], vah=[high,high+BIN_SIZE], val=[low,low+BIN_SIZE],
                hvn=[[p-BIN_SIZE,p+BIN_SIZE] for p in hvn], lvn=[[p-BIN_SIZE,p+BIN_SIZE] for p in lvn],
                method='USD 1 bins; 70% value area; HVN local maxima >=10% POC; LVN local minima <=3.5% POC')
    if owned: db.close()
    return result


def status(path=DATABASE):
    db = connect(path); now = now_ms(); start = session_start_ms(now)
    first,last,count = db.execute('SELECT MIN(event_time),MAX(event_time),COUNT(*) FROM trades WHERE event_time>=?',(start,)).fetchone()
    return dict(symbol=SYMBOL, database=str(path), session_start=iso(start), trades=count, first_trade=iso(first) if first else None,
                last_trade=iso(last) if last else None, lag_seconds=(now-last)/1000 if last else None,
                bootstrap_complete=state(db,'bootstrap_complete'), bootstrap_imported=state(db,'bootstrap_imported'),
                bootstrap_error=state(db,'bootstrap_error'), bootstrap_session_start=state(db,'bootstrap_session_start'))


async def collect(path=DATABASE):
    db = connect(path)
    try:
        bootstrap(db)
    except Exception as exc:
        state(db, 'bootstrap_error', repr(exc))
    delay = 1
    while True:
        try:
            async with websockets.connect(STREAM, ping_interval=20, ping_timeout=20, max_size=1_000_000) as socket:
                delay = 1
                async for raw in socket:
                    save(db, json.loads(raw)); db.commit()
        except (OSError, ValueError, json.JSONDecodeError, websockets.WebSocketException) as exc:
            state(db, 'stream_error', repr(exc)); await asyncio.sleep(delay); delay=min(delay*2,60)


def main():
    parser=argparse.ArgumentParser(description='ETHUSDT public trade collector')
    parser.add_argument('--status', action='store_true'); parser.add_argument('--bootstrap-only', action='store_true')
    args=parser.parse_args()
    if args.status: print(json.dumps(status(), ensure_ascii=False)); return
    db=connect()
    if args.bootstrap_only: print(json.dumps({'imported':bootstrap(db), **status()}, ensure_ascii=False)); return
    asyncio.run(collect())


if __name__ == '__main__': main()
