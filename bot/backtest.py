"""Leakage-free ETHUSDT research backtest with archived Binance trades and 1m execution paths.

This is a research proxy for VP/SMC components: it makes every reconstructed
zone deterministic. It must not be called a validation of discretionary SMC.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .engine import atr_series, pivots

ROOT = Path(__file__).resolve().parents[1] / 'private-data' / 'backtest'
FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
RISK_FRACTION = .01
VALUE_AREA = .70


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def weighted_vwap(bars):
    quote, volume = sum(b['quote'] for b in bars), sum(b['volume'] for b in bars)
    return quote / volume if volume else None


def session_bars(bars, index):
    """UTC session only, matching Session Volume Profile and session VWAP."""
    start = bars[index]['t'] // 86400000 * 86400000
    return [bar for bar in bars[:index+1] if bar['t'] >= start]


def volume_profile(bars):
    histogram = defaultdict(float)
    for bar in bars:
        for price, amount in bar['hist'].items():
            histogram[int(price)] += amount
    if not histogram:
        return None
    poc = max(histogram, key=lambda price: (histogram[price], -price))
    target, included = sum(histogram.values()) * VALUE_AREA, {poc}
    running = histogram[poc]
    low = high = poc
    while running < target:
        left, right = histogram.get(low - 1, -1), histogram.get(high + 1, -1)
        if left < 0 and right < 0:
            break
        if right > left:
            high += 1; running += max(right, 0); included.add(high)
        else:
            low -= 1; running += max(left, 0); included.add(low)
    # Local maxima with at least 10% POC volume are HVN zones, not every bin.
    threshold = histogram[poc] * .10
    hvn = [p for p in sorted(histogram) if histogram[p] >= threshold and
           histogram[p] >= histogram.get(p-1, 0) and histogram[p] >= histogram.get(p+1, 0)]
    lvn = [p for p in range(min(histogram)+1, max(histogram)) if histogram[p] <= threshold*.35 and
           histogram[p] <= histogram.get(p-1, math.inf) and histogram[p] <= histogram.get(p+1, math.inf)]
    return dict(poc=poc, poc_zone=(poc-1, poc+1), vah=high, val=low,
                hvn=[(p-1, p+1) for p in hvn], lvn=[(p-1, p+1) for p in lvn])


def unmitigated_fvg(bars, index, direction):
    candidates = []
    for i in range(2, index):
        if direction == 'LONG' and bars[i-2]['high'] < bars[i]['low']:
            zone = (bars[i-2]['high'], bars[i]['low'])
        elif direction == 'SHORT' and bars[i]['high'] < bars[i-2]['low']:
            zone = (bars[i]['high'], bars[i-2]['low'])
        else:
            continue
        # Any overlap after the FVG exists mitigates it. Current sweep is first touch.
        if not any(b['low'] <= zone[1] and b['high'] >= zone[0] for b in bars[i+1:index]):
            candidates.append(zone)
    return candidates


def unmitigated_order_blocks(bars, index, direction):
    """Mechanical proxy: last opposite candle before a confirmed 3-bar imbalance.

    The zone is the candle body (not the entire wick) and it is invalidated by
    the first later overlap. This makes the discretionary term testable.
    """
    candidates = []
    for i in range(2, index):
        imbalance = (direction == 'LONG' and bars[i-2]['high'] < bars[i]['low']) or \
                    (direction == 'SHORT' and bars[i]['high'] < bars[i-2]['low'])
        if not imbalance:
            continue
        opposite = (lambda b: b['close'] < b['open']) if direction == 'LONG' else (lambda b: b['close'] > b['open'])
        source = next((j for j in range(i-1, max(-1, i-6), -1) if opposite(bars[j])), None)
        if source is None:
            continue
        zone = tuple(sorted((bars[source]['open'], bars[source]['close'])))
        if zone[0] == zone[1]:
            continue
        # The OB is confirmed only when the later imbalance completes at i.
        if not any(b['low'] <= zone[1] and b['high'] >= zone[0] for b in bars[i+1:index]):
            candidates.append(zone)
    return candidates


def clean_sweep(bars, index, direction):
    preceding = bars[:index]
    points = pivots(preceding, 'low' if direction == 'LONG' else 'high', direction == 'SHORT')
    if not points:
        return None
    point = points[-1]
    level = preceding[point]['low' if direction == 'LONG' else 'high']
    current = bars[index]
    broke = current['low'] < level < current['close'] if direction == 'LONG' else current['close'] < level < current['high']
    already = any(b['low'] < level if direction == 'LONG' else b['high'] > level for b in bars[point+1:index])
    return (point, level) if broke and not already else None


def signal(bars, index, previous_poc, min_atr_ratio=.5):
    """Only bars <= index are observed. Returns a fully auditable research signal."""
    if index < 35:
        return None
    current, seen = bars[index], bars[:index+1]
    atrs = atr_series(seen)
    if len(atrs) < 21:
        return None
    atr, base = atrs[-1], sum(atrs[-21:-1]) / 20
    if not base or atr / base < min_atr_ratio:
        return None
    session = session_bars(bars, index)
    profile, vwap = volume_profile(session), weighted_vwap(session)
    if not profile or not vwap:
        return None
    for direction in ('LONG', 'SHORT'):
        sweep = clean_sweep(bars, index, direction)
        if not sweep:
            continue
        pivot, level = sweep
        sign = 1 if direction == 'LONG' else -1
        # CVD does not make the price extreme: exactly the original divergence condition.
        if (current['cvd'] < bars[pivot]['cvd'] if direction == 'LONG' else current['cvd'] > bars[pivot]['cvd']):
            continue
        avg_volume = sum(b['volume'] for b in bars[index-20:index]) / 20
        if current['volume'] <= avg_volume * 1.5:
            continue
        active = unmitigated_fvg(bars, index, direction) + unmitigated_order_blocks(bars, index, direction)
        price = current['close']
        # The original rule requires an OB/FVG *in the working area*; it does
        # not require the close to sit inside that zone after the sweep.
        zone = next((z for z in active if max(z[0], profile['poc_zone'][0]) <= min(z[1], profile['poc_zone'][1])), None)
        if not zone or not profile['poc_zone'][0] <= price <= profile['poc_zone'][1]:
            continue
        if max(abs(x-vwap)/x*100 for x in profile['poc_zone']) >= .3:
            continue
        if (direction == 'LONG' and not price > vwap) or (direction == 'SHORT' and not price < vwap):
            continue
        if previous_poc is not None and ((direction == 'LONG' and profile['poc'] < previous_poc) or (direction == 'SHORT' and profile['poc'] > previous_poc)):
            continue
        targets = sorted([z[0] for z in profile['hvn'] if sign*(z[0]-price) > 0] +
                         ([vwap] if sign*(vwap-price) > 0 else []), reverse=sign < 0)
        lvns = sorted([z[0] for z in profile['lvn'] if targets and sign*(z[0]-targets[0]) > 0], reverse=sign < 0)
        if not targets or not lvns:
            continue
        entry = price * (1 + sign*SLIPPAGE_BPS/10000)
        stop = current['low']-atr if sign == 1 else current['high']+atr
        risk = abs(entry-stop)
        tp1, tp2 = targets[0], lvns[0]
        rr = (sign*(tp1-entry)/risk + sign*(tp2-entry)/risk) / 2
        if rr < 2:
            continue
        return dict(direction=direction, index=index, time=current['t'], entry=entry, stop=stop,
                    tp1=tp1, tp2=tp2, rr=rr, atr=atr, pivot_time=bars[pivot]['t'],
                    sweep_level=level, avg_volume=avg_volume, profile=profile, fvg=zone,
                    vwap=vwap, cvd_pivot=bars[pivot]['cvd'], cvd_current=current['cvd'])
    return None


def execute(trade, minutes, close_time):
    """Entry is after 15m close; then 1m OHLC. Same-minute stop wins conservatively."""
    sign = 1 if trade['direction'] == 'LONG' else -1
    quantity = RISK_FRACTION / abs(trade['entry']-trade['stop'])
    remaining, realized = 1., 0.
    fees = abs(trade['entry'])*quantity*FEE_BPS/10000
    tp1_hit = False
    exit_time = close_time
    exit_price, reason = None, 'SESSION_CLOSE'
    for minute in minutes:
        if minute['t'] <= close_time:
            continue
        hit_stop = minute['l'] <= trade['stop'] if sign == 1 else minute['h'] >= trade['stop']
        hit_tp1 = minute['h'] >= trade['tp1'] if sign == 1 else minute['l'] <= trade['tp1']
        hit_tp2 = minute['h'] >= trade['tp2'] if sign == 1 else minute['l'] <= trade['tp2']
        # Ambiguous OHLC path is adverse: prevents a best-case fill assumption.
        if hit_stop:
            exit_price, exit_time, reason = trade['stop']*(1-sign*SLIPPAGE_BPS/10000), minute['t'], 'STOP'
            realized += remaining * sign*(exit_price-trade['entry']); fees += abs(exit_price)*quantity*remaining*FEE_BPS/10000
            remaining = 0; break
        if hit_tp1 and not tp1_hit:
            price = trade['tp1']*(1-sign*SLIPPAGE_BPS/10000)
            realized += .5*sign*(price-trade['entry']); fees += abs(price)*quantity*.5*FEE_BPS/10000
            remaining = .5; tp1_hit = True
        if hit_tp2:
            exit_price, exit_time, reason = trade['tp2']*(1-sign*SLIPPAGE_BPS/10000), minute['t'], 'TP2'
            realized += remaining*sign*(exit_price-trade['entry']); fees += abs(exit_price)*quantity*remaining*FEE_BPS/10000
            remaining = 0; break
    if remaining:
        final = minutes[-1]['c']*(1-sign*SLIPPAGE_BPS/10000)
        realized += remaining*sign*(final-trade['entry']); fees += abs(final)*quantity*remaining*FEE_BPS/10000
        exit_price = final
    pnl_r = (realized*quantity-fees)/RISK_FRACTION
    return dict(**trade, exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
                tp1_hit=tp1_hit, pnl_r=pnl_r, fees_usdt=fees, quantity=quantity)


def funnel(bars):
    """Count candidate-direction pairs passing each sequential gate for diagnosis."""
    stages = ['bars_checked', 'sweep', 'cvd', 'volume', 'atr', 'poc', 'vwap', 'fvg', 'targets', 'rr']
    counts = dict.fromkeys(stages, 0)
    previous_poc, day = None, None
    for index, current in enumerate(bars):
        current_day = current['t']//86400000
        if current_day != day:
            day, previous_poc = current_day, None
        profile = volume_profile(session_bars(bars, index))
        if profile:
            old_poc, previous_poc = previous_poc, profile['poc']
        else:
            old_poc = previous_poc
        if index < 35 or not profile:
            continue
        atrs = atr_series(bars[:index+1])
        for direction in ('LONG', 'SHORT'):
            counts['bars_checked'] += 1
            sweep = clean_sweep(bars, index, direction)
            if not sweep: continue
            counts['sweep'] += 1
            pivot, _level = sweep
            if current['cvd'] < bars[pivot]['cvd'] if direction == 'LONG' else current['cvd'] > bars[pivot]['cvd']: continue
            counts['cvd'] += 1
            avg = sum(b['volume'] for b in bars[index-20:index])/20
            if current['volume'] <= 1.5*avg: continue
            counts['volume'] += 1
            if len(atrs)<21 or not atrs[-21:-1] or atrs[-1]/(sum(atrs[-21:-1])/20) < .5: continue
            counts['atr'] += 1
            price, sign = current['close'], 1 if direction=='LONG' else -1
            if not profile['poc_zone'][0] <= price <= profile['poc_zone'][1]: continue
            if old_poc is not None and ((sign==1 and profile['poc'] < old_poc) or (sign==-1 and profile['poc'] > old_poc)): continue
            counts['poc'] += 1
            vwap=weighted_vwap(session_bars(bars,index))
            if max(abs(x-vwap)/x*100 for x in profile['poc_zone']) >= .3 or sign*(price-vwap)<=0: continue
            counts['vwap'] += 1
            active=unmitigated_fvg(bars,index,direction)+unmitigated_order_blocks(bars,index,direction)
            if not any(max(z[0],profile['poc_zone'][0]) <= min(z[1],profile['poc_zone'][1]) for z in active): continue
            counts['fvg'] += 1
            first=sorted([z[0] for z in profile['hvn'] if sign*(z[0]-price)>0]+([vwap] if sign*(vwap-price)>0 else []),reverse=sign<0)
            second=sorted([z[0] for z in profile['lvn'] if first and sign*(z[0]-first[0])>0],reverse=sign<0)
            if not first or not second: continue
            counts['targets'] += 1
            risk=abs(price-(current['low']-atrs[-1] if sign==1 else current['high']+atrs[-1]))
            if ((sign*(first[0]-price)/risk)+(sign*(second[0]-price)/risk))/2 >=2: counts['rr'] += 1
    return counts


def load_days(start, end):
    bars, minutes, audits = [], [], []
    for path in sorted((ROOT/'prepared').glob('*.json')):
        day = path.stem
        if start <= day <= end:
            body = json.loads(path.read_text())
            bars += body['bars']; minutes += body['minutes']; audits.append(body['audit'])
    if not bars:
        raise ValueError('No prepared history: run python -m bot.history first')
    bars.sort(key=lambda x: x['t']); minutes.sort(key=lambda x: x['t'])
    if any(b['t']-a['t'] != 900000 for a, b in zip(bars,bars[1:])):
        raise ValueError('Gap in 15m archive')
    cvd = 0
    for bar in bars:
        if bar['t'] % 86400000 == 0:
            cvd = 0
        cvd += bar['delta']; bar['cvd'] = cvd
    return bars, minutes, audits


def run(start, end, calendar='strict'):
    bars, minutes, audits = load_days(start, end)
    if calendar != 'research_bypass':
        report = dict(status='NOT_EXECUTED', mode='strict', reason='Historical high-impact economic calendar is not supplied; rules require its confirmation.', trades=[], audits=audits)
    else:
        trades, previous_poc, day, last_exit_t = [], None, None, -1
        for i, bar in enumerate(bars):
            current_day = datetime.fromtimestamp(bar['t']/1000, timezone.utc).date().isoformat()
            if current_day != day:
                day, previous_poc = current_day, None
            candidate = signal(bars, i, previous_poc)
            profile = volume_profile(session_bars(bars, i))
            if profile: previous_poc = profile['poc']
            if candidate and bar['t'] > last_exit_t:
                session_end = (bar['t']//86400000 + 1)*86400000
                path = [m for m in minutes if bar['t'] < m['t'] < session_end]
                if path:
                    completed = execute(candidate, path, bar['t'])
                    trades.append(completed)
                    last_exit_t = completed['exit_time']
        pnl = [t['pnl_r'] for t in trades]
        equity, peak, max_dd, curve = 1., 1., 0., []
        for r in pnl:
            equity *= 1+r*RISK_FRACTION; peak=max(peak,equity); max_dd=max(max_dd,1-equity/peak); curve.append(equity)
        report = dict(status='RESEARCH_ONLY', mode='research_bypass', assumptions=dict(
            calendar='BYPASSED: results cannot validate the deployable strategy',
            profile='exact archived trades, integer USD bins, 70% value area',
            smart_money='mechanical unmitigated 3-bar FVG or last opposite candle body before such imbalance',
            execution='next 1m after signal close; adverse same-minute conflict; session-close exit',
            fees_bps_per_side=FEE_BPS, slippage_bps_per_fill=SLIPPAGE_BPS, risk_fraction=RISK_FRACTION),
            period=dict(start=start,end=end,bars=len(bars),minutes=len(minutes)), audits=audits, funnel=funnel(bars), trades=trades,
            metrics=dict(trades=len(trades), net_r=sum(pnl), win_rate=(sum(x>0 for x in pnl)/len(pnl) if pnl else None),
                         profit_factor=(sum(x for x in pnl if x>0)/abs(sum(x for x in pnl if x<0)) if any(x<0 for x in pnl) else None),
                         max_drawdown_fraction=max_dd, ending_equity=equity, curve=curve))
    output = ROOT/'reports'/f'ethusdt-15m-{start}-to-{end}-{calendar}.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report, output


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--start',default='2026-09-01'); p.add_argument('--end',default='2026-09-29'); p.add_argument('--calendar',choices=('strict','research_bypass'),default='strict'); a=p.parse_args()
    report, output=run(a.start,a.end,a.calendar); print(json.dumps(dict(path=str(output),status=report['status'],metrics=report.get('metrics')),ensure_ascii=False))
