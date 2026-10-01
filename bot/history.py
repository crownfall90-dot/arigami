"""Checksum-verified ETH archives, exact trade-price histograms, no OHLCV profile proxies."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, timedelta
import hashlib
import io
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1] / 'private-data' / 'backtest'
BASE = 'https://data.binance.vision/data/futures/um'


def download(url):
    cache = ROOT / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit('/', 1)[-1]
    def request(value, timeout):
        for attempt in range(4):
            try:
                return urlopen(value, timeout=timeout)
            except (HTTPError, URLError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    with request(url + '.CHECKSUM', 40) as response:
        expected = response.read().decode().split()[0]
    if not path.exists():
        temporary = path.with_suffix('.part')
        with request(url, 60) as response, temporary.open('wb') as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        temporary.replace(path)
    actual = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    if actual != expected:
        raise ValueError(f'Checksum mismatch: {path}')
    return path, dict(url=url, sha256=actual, bytes=path.stat().st_size)


def rows(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError('Expected one CSV per archive')
        with archive.open(names[0]) as source:
            reader = csv.reader(io.TextIOWrapper(source, encoding='utf-8'))
            next(reader)  # Verified Binance futures CSV header.
            yield from reader


def prepare_day(day):
    output = ROOT / 'prepared' / f'{day}.json'
    if output.exists():
        return json.loads(output.read_text())['audit']
    trade_path, trade_manifest = download(f'{BASE}/daily/aggTrades/ETHUSDT/ETHUSDT-aggTrades-{day}.zip')
    candle_path, candle_manifest = download(f'{BASE}/daily/klines/ETHUSDT/1m/ETHUSDT-1m-{day}.zip')
    minutes = []
    for row in rows(candle_path):
        minutes.append(dict(t=int(row[0]), o=float(row[1]), h=float(row[2]), l=float(row[3]),
                            c=float(row[4]), v=float(row[5]), q=float(row[7]), buy=float(row[9])))
    if len(minutes) != 1440 or any(b['t']-a['t'] != 60000 for a, b in zip(minutes, minutes[1:])):
        raise ValueError(f'{day}: missing/duplicated minute candles')
    start = minutes[0]['t']
    if start % 86400000:
        raise ValueError('UTC midnight expected')
    histograms = [defaultdict(float) for _ in range(96)]
    deltas = [0.] * 96
    count, previous_id, previous_time = 0, -1, -1
    for r in rows(trade_path):
        trade_id, price, qty, time = int(r[0]), float(r[1]), float(r[2]), int(r[5])
        if trade_id <= previous_id or time < previous_time or price <= 0 or qty <= 0 or r[6] not in ('true', 'false'):
            raise ValueError(f'{day}: invalid trade ordering/values')
        bucket = (time-start)//900000
        if not 0 <= bucket < 96:
            raise ValueError('Trade outside UTC session')
        histograms[bucket][int(price)] += qty  # $1 bins, never synthetic candle allocation.
        deltas[bucket] += -qty if r[6] == 'true' else qty
        count += 1
        previous_id, previous_time = trade_id, time
    bars, errors, delta_errors = [], [], []
    for i in range(96):
        group = minutes[i*15:(i+1)*15]
        volume = sum(c['v'] for c in group)
        trade_volume = sum(histograms[i].values())
        error = abs(trade_volume-volume)/max(volume, 1e-12)
        delta_error = abs(deltas[i]-sum(2*c['buy']-c['v'] for c in group))/max(volume, 1e-12)
        errors.append(error)
        delta_errors.append(delta_error)
        if max(error, delta_error) > .005:
            raise ValueError(f'{day} bar {i}: trades and candles disagree >0.5%: {error}, {delta_error}')
        bars.append(dict(t=group[0]['t'], open=group[0]['o'], high=max(c['h'] for c in group),
                         low=min(c['l'] for c in group), close=group[-1]['c'], volume=volume,
                         quote=sum(c['q'] for c in group), delta=deltas[i], hist=dict(histograms[i])))
    audit = dict(day=day, minutes=len(minutes), bars=len(bars), trades=count,
                 max_volume_error=max(errors), max_delta_error=max(delta_errors),
                 archives=[trade_manifest, candle_manifest])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(bars=bars, minutes=minutes, audit=audit)), encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False), flush=True)
    return audit


def prepare(start, end, workers=1):
    days = []
    day = date.fromisoformat(start)
    while day <= date.fromisoformat(end):
        days.append(str(day))
        day += timedelta(days=1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        audits = list(pool.map(prepare_day, days))
    path, funding_manifest = download(f'{BASE}/monthly/fundingRate/ETHUSDT/ETHUSDT-fundingRate-{start[:7]}.zip')
    funding = [dict(t=int(r[0]), rate=float(r[2])) for r in rows(path)]
    result = dict(start=start, end=end, days=audits, funding=funding, funding_archive=funding_manifest)
    (ROOT / 'manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2026-09-01')
    p.add_argument('--end', default='2026-09-29')
    p.add_argument('--workers', type=int, default=1)
    args = p.parse_args()
    prepare(args.start, args.end, args.workers)
