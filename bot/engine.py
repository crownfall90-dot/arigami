"""Deterministic, fail-closed analysis of supplied market evidence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import math


class EvidenceError(ValueError):
    pass


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvidenceError(f"{label}: требуется конечное число")
    if positive and value <= 0:
        raise EvidenceError(f"{label}: требуется число > 0")
    return value


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise EvidenceError("Время должно быть ISO 8601 с часовым поясом") from None


def zone(value, label):
    if not isinstance(value, list) or len(value) != 2:
        raise EvidenceError(f"{label}: требуется [нижняя, верхняя граница]")
    low, high = (number(v, label, True) for v in value)
    if low > high:
        raise EvidenceError(f"{label}: границы переставлены")
    return low, high


def inside(price, bounds):
    return bounds[0] <= price <= bounds[1]


def atr_series(candles, period=14):
    ranges = [max(c['high'] - c['low'], abs(c['high'] - p['close']),
                  abs(c['low'] - p['close'])) for p, c in zip(candles, candles[1:])]
    if len(ranges) < period:
        return []
    values = [sum(ranges[:period]) / period]
    for value in ranges[period:]:
        values.append((values[-1] * (period - 1) + value) / period)
    return values


def pivots(candles, key, high):
    result = []
    for i in range(2, len(candles) - 2):
        other = [candles[j][key] for j in (i-2, i-1, i+1, i+2)]
        if (candles[i][key] > max(other) if high else candles[i][key] < min(other)):
            result.append(i)
    return result


def analyze(data):
    try:
        return _analyze(data)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        result = _analyze({})
        result['missing'] = [f'Некорректные входные данные: {exc}']
        return result


def _analyze(data):
    names = ['HVN/POC', 'VWAP < 0.3%', 'OB/FVG', 'Liquidity Sweep',
             'CVD divergence', 'Sweep Volume > 1.5× Avg20', 'RR ≥ 1:2']
    result = dict(system='VP-SMC-CVD «Тройное подтверждение»', decision='ПРОПУСК',
                  checks=[dict(name=n, passed=False, detail='НЕТ ДАННЫХ') for n in names],
                  context={}, filters={}, missing=[], reasons=[], trade=None,
                  position={'status': 'НЕТ ДАННЫХ'})
    if not isinstance(data, dict):
        result['reasons'] = ['Входные данные должны быть JSON-объектом']
        return result

    def check(index, passed, detail):
        result['checks'][index].update(passed=bool(passed), detail=detail)

    def evidence(label, work):
        try:
            return work()
        except (KeyError, TypeError, IndexError, EvidenceError) as exc:
            result['missing'].append(f'{label}: {exc}')
            return None

    def base():
        if not all(isinstance(data[k], str) and data[k].strip() for k in ('symbol', 'exchange', 'source')):
            raise EvidenceError('symbol, exchange, source обязательны')
        tf = data['timeframe_minutes']
        if tf not in (15, 30):
            raise EvidenceError('Поддерживаются только 15 и 30 минут')
        now = stamp(data['as_of'])
        candles = data['candles']
        if not isinstance(candles, list) or len(candles) < 35:
            raise EvidenceError('Нужно минимум 35 закрытых свечей OHLCV для ATR и его базы')
        previous = None
        for c in candles:
            for k in ('open', 'high', 'low', 'close'):
                number(c[k], k, True)
            number(c['volume'], 'volume')
            if c['volume'] < 0 or c['low'] > min(c['open'], c['close']) or c['high'] < max(c['open'], c['close']):
                raise EvidenceError('Некорректная свеча OHLCV')
            when = stamp(c['close_time'])
            if c.get('closed') is not True or when > now:
                raise EvidenceError('Свечи должны быть закрыты на момент as_of')
            if previous and when - previous != timedelta(minutes=tf):
                raise EvidenceError('Свечи должны идти подряд без пропусков и дублей')
            previous = when
        if not 0 <= (now - previous).total_seconds() <= 60:
            raise EvidenceError('Сигнал устарел: последняя свеча закрылась более 60 секунд назад')
        return candles, now, tf

    loaded = evidence('OHLCV / метаданные', base)
    if loaded is None:
        result['reasons'] = ['ПРОПУСК — НЕДОСТАТОЧНО ДАННЫХ']
        return result
    candles, now, tf = loaded
    last = candles[-1]
    price = last['close']
    context = result['context']
    context.update(symbol=data['symbol'], exchange=data['exchange'], timeframe=tf, price=price,
                   regime='НЕОПРЕДЕЛЁННЫЙ', as_of=data['as_of'])
    highs = pivots(candles[:-1], 'high', True)
    lows = pivots(candles[:-1], 'low', False)
    structure = 'НЕОПРЕДЕЛЁННАЯ'
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = [candles[i]['high'] for i in highs[-2:]]
        l1, l2 = [candles[i]['low'] for i in lows[-2:]]
        if h2 > h1 and l2 > l1:
            structure = 'HH/HL'
        elif h2 < h1 and l2 < l1:
            structure = 'LH/LL'
        elif h2 <= h1 and l2 >= l1:
            structure = 'БОКОВИК'
    context['structure'] = structure
    candidates = []
    for direction, indexes, key in [('LONG', lows, 'low'), ('SHORT', highs, 'high')]:
        if indexes:
            index = indexes[-1]
            level = candles[index][key]
            swept = last['low'] < level < price if direction == 'LONG' else price < level < last['high']
            if swept:
                candidates.append((direction, index, level))
    direction, pivot, level = candidates[0] if len(candidates) == 1 else (None, None, None)
    context['liquidity'] = {'direction': direction, 'level': level, 'pivot_index': pivot}
    check(3, direction is not None, f'{direction}: возврат за уровень {level} после закрытия' if direction else
          'Нет однозначного sweep последнего подтверждённого локального экстремума')

    def profile():
        p = data['profile']
        expected = 'session' if tf == 15 else 'fixed_range'
        if p['type'] != expected or not p['source']:
            raise EvidenceError(f'Нужен профиль {expected} и источник')
        if stamp(p['start']) >= stamp(p['end']) or stamp(p['end']) != stamp(last['close_time']):
            raise EvidenceError('Диапазон профиля должен завершаться на последней закрытой свече')
        out = {k: zone(p[k], k) for k in ('poc', 'vah', 'val', 'previous_poc')}
        out.update({k: [zone(z, k) for z in p[k]] for k in ('hvn', 'lvn')})
        if not out['hvn'] or not out['lvn'] or out['val'][1] > out['vah'][0]:
            raise EvidenceError('Нужны HVN, LVN и корректная область стоимости')
        if not out['val'][0] <= sum(out['poc']) / 2 <= out['vah'][1]:
            raise EvidenceError('POC должен находиться в области стоимости')
        context.update(out)
        working = [z for z in [out['poc']] + out['hvn'] if inside(price, z)]
        check(0, bool(working), f'Цена {price}; рабочие зоны: {working}')
        return out, working[0] if working else None

    prof = evidence('Volume Profile', profile)
    def vwap():
        v = number(data['vwap']['value'], 'VWAP', True)
        if not data['vwap']['source'] or stamp(data['vwap']['as_of']) != stamp(last['close_time']):
            raise EvidenceError('Нужен VWAP на время последней закрытой свечи и источник')
        context['vwap'] = v
        if prof and prof[1]:
            # Worst-case distance over the entire zone avoids cherry-picking a midpoint.
            distance = max(abs(x-v)/x*100 for x in prof[1])
            check(1, distance < .3, f'Максимальное расстояние по границам зоны: {distance:.6f}%')
        return v
    vw = evidence('VWAP', vwap)

    def smart_money():
        matches = []
        zones = data['smart_money']
        if not isinstance(zones, list):
            raise EvidenceError('Нужен список подтверждённых OB/FVG')
        for z in zones:
            bounds = zone(z['zone'], 'OB/FVG')
            created = stamp(z['created_at'])
            if z['kind'] not in ('OB', 'FVG') or z['direction'] not in ('LONG', 'SHORT'):
                raise EvidenceError('Неверный вид или направление OB/FVG')
            if created > now or created < stamp(candles[0]['close_time']):
                raise EvidenceError('История свечей должна покрывать жизнь OB/FVG')
            # Any previous revisit invalidates an unmitigated zone; the sweep is its first touch.
            revisited = any(c['low'] <= bounds[1] and c['high'] >= bounds[0]
                            for c in candles[:-1] if stamp(c['close_time']) > created)
            if (z['confirmed'] is True and z['unmitigated'] is True and z['source']
                    and not revisited and z['direction'] == direction
                    and inside(price, bounds) and prof and prof[1]
                    and max(bounds[0], prof[1][0]) <= min(bounds[1], prof[1][1])):
                matches.append(z)
        context['smart_money'] = matches
        check(2, bool(matches), f'Актуальные зоны в рабочей области: {len(matches)}')
    evidence('OB/FVG', smart_money)

    def cvd():
        c = data['cvd']
        if not c['source'] or c['method'] != 'aggressor_trades':
            raise EvidenceError('Нужен CVD по стороне агрессора; OHLCV-прокси не принимается')
        values = c['values']
        if len(values) != len(candles):
            raise EvidenceError('CVD должен соответствовать каждой свече')
        for candle, point in zip(candles, values):
            if stamp(point['close_time']) != stamp(candle['close_time']):
                raise EvidenceError('Времена CVD и OHLCV не совпадают')
            number(point['value'], 'CVD')
        context['cvd'] = values[-1]['value']
        if direction:
            old, new = values[pivot]['value'], values[-1]['value']
            passed = new >= old if direction == 'LONG' else new <= old
            check(4, passed, f'CVD на опорном экстремуме: {old}; на sweep: {new}')
    evidence('CVD', cvd)
    average = sum(c['volume'] for c in candles[-21:-1])/20
    ratio = last['volume']/average if average > 0 else None
    context['volume'] = {'sweep': last['volume'], 'avg20': average, 'ratio': ratio}
    check(5, average > 0 and last['volume'] > average*1.5,
          f'Sweep Volume: {last["volume"]}; Avg20: {average}; Ratio: {ratio}')

    atrs = atr_series(candles)
    atr = atrs[-1]
    baseline = sum(atrs[-21:-1])/20
    context.update(atr=atr, atr_baseline=baseline)
    def volatility():
        threshold = number(data['policy']['min_atr_ratio'], 'min_atr_ratio', True)
        if threshold > 1:
            raise EvidenceError('min_atr_ratio должен быть <= 1')
        return baseline > 0 and atr > 0 and atr/baseline >= threshold
    vol = evidence('Порог низкой волатильности', volatility)
    result['filters']['atr'] = {'passed': vol is True, 'detail': f'ATR(14) Wilder: {atr}; база предыдущих 20 ATR: {baseline}'}

    def news():
        n = data['news']
        if n['verified'] is not True or not n['source']:
            raise EvidenceError('Новостной фильтр: НЕ ПОДТВЕРЖДЁН')
        if not 0 <= (now-stamp(n['checked_at'])).total_seconds() <= 900:
            raise EvidenceError('Проверка календаря устарела или находится в будущем')
        if stamp(n['coverage_start']) > now or stamp(n['coverage_end']) < now+timedelta(minutes=15):
            raise EvidenceError('Календарь должен покрывать следующие 15 минут')
        for e in n['events']:
            if not isinstance(e['relevant'], bool) or e['impact'] not in ('high', 'medium', 'low'):
                raise EvidenceError('Нужны relevance и impact событий')
            delta = (stamp(e['time'])-now).total_seconds()
            if e['relevant'] and e['impact'] == 'high' and 0 <= delta < 900:
                return False
        return True
    news_ok = evidence('Экономический календарь', news)
    result['filters']['news'] = {'passed': news_ok is True,
                                'detail': 'Подтверждён' if news_ok is True else ('Событие менее чем через 15 минут' if news_ok is False else 'НЕ ПОДТВЕРЖДЁН')}

    context_ok = False
    if prof and vw is not None and direction:
        p = prof[0]
        delta = sum(p['poc'])/2-sum(p['previous_poc'])/2
        if structure == 'HH/HL' and price > vw and delta >= 0:
            context['regime'] = 'ВОСХОДЯЩИЙ ТРЕНД'
        elif structure == 'LH/LL' and price < vw and delta <= 0:
            context['regime'] = 'НИСХОДЯЩИЙ ТРЕНД'
        elif structure == 'БОКОВИК':
            context['regime'] = 'БОКОВИК'
        context['poc_shift'] = delta
        reaction = result['checks'][3]['passed'] and result['checks'][4]['passed']
        context_ok = ((price > vw or inside(price, p['val']) and reaction) if direction == 'LONG'
                      else (price < vw or inside(price, p['vah']) and reaction))
        if direction == 'LONG' and structure == 'LH/LL' and delta < 0:
            context_ok = False
        if direction == 'SHORT' and structure == 'HH/HL' and delta > 0:
            context_ok = False
    result['filters']['direction'] = {'passed': context_ok, 'detail': 'Контекст направления / запрет против тренда и POC'}

    if direction and prof and vw is not None and atr > 0:
        sign = 1 if direction == 'LONG' else -1
        sl = last['low']-atr if sign == 1 else last['high']+atr
        risk = abs(price-sl)
        # Near edge of target zone; use only targets beyond entry in trade direction.
        targets = [z[0] if sign == 1 else z[1] for z in prof[0]['hvn']] + [vw]
        targets = sorted([v for v in targets if sign*(v-price) > 0], reverse=sign == -1)
        if targets and sl > 0:
            tp1 = targets[0]
            second = [z[0] if sign == 1 else z[1] for z in prof[0]['lvn']]
            second = sorted([v for v in second if sign*(v-tp1) > 0], reverse=sign == -1)
            if second:
                tp2 = second[0]
                rr1, rr2 = sign*(tp1-price)/risk, sign*(tp2-price)/risk
                rr = (rr1+rr2)/2
                check(6, rr >= 2, f'RR TP1: {rr1:.4f}; RR TP2: {rr2:.4f}; RR 50/50: {rr:.4f}')
                result['trade'] = dict(direction=direction, entry=price, stop_loss=sl, tp1=tp1, tp2=tp2,
                                       stop_distance=risk, stop_percent=risk/price*100, rr=rr, rr_tp1=rr1,
                                       rr_tp2=rr2, allocation=[.5, .5], size=None,
                                       size_note='Размер позиции: невозможно рассчитать без размера депозита.')
                if 'account' in data:
                    def sizing():
                        a = data['account']
                        deposit = number(a['deposit'], 'deposit', True)
                        multiplier = number(a['contract_multiplier'], 'contract_multiplier', True)
                        step = number(a['quantity_step'], 'quantity_step', True)
                        if a['contract_type'] != 'linear' or a['deposit_currency'] != a['quote_currency']:
                            raise EvidenceError('Нужен линейный контракт и депозит в валюте котировки')
                        fee = number(a['taker_fee_bps'], 'taker_fee_bps')
                        slip = number(a['slippage_bps'], 'slippage_bps')
                        if min(fee, slip) < 0:
                            raise EvidenceError('Издержки не могут быть отрицательными')
                        costs = (price+sl)*(fee+slip)/10000
                        raw = deposit*.01/((risk+costs)*multiplier)
                        size = float((Decimal(str(raw))/Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(step)))
                        result['trade'].update(size=size, deposit=deposit, risk_budget=deposit*.01,
                                               estimated_risk=size*(risk+costs)*multiplier,
                                               size_note='Количество контрактов; округлено вниз, с резервом издержек')
                        return size > 0
                    size_ok = evidence('Размер позиции', sizing)
                    result['filters']['sizing'] = {'passed': size_ok is True, 'detail': 'Размер в пределах 1% депозита'}

    approved = all(c['passed'] for c in result['checks']) and all(f['passed'] for f in result['filters'].values()) and not result['missing']
    if approved:
        result['decision'] = direction
    else:
        result['reasons'] = [c['name']+': '+c['detail'] for c in result['checks'] if not c['passed']]
        result['reasons'] += [f['detail'] for f in result['filters'].values() if not f['passed']]
        if result['missing']:
            result['reasons'].insert(0, 'ПРОПУСК — НЕДОСТАТОЧНО ДАННЫХ')
        # Do not publish actionable entries for a rejected setup.
        result['trade'] = None
    result['position'] = manage_position(data.get('position'), context)
    if isinstance(data.get('position'), dict):
        result['decision'] = 'ПРОПУСК'
        result['trade'] = None
        result['reasons'].append('Позиция уже существует: повторный вход и усреднение запрещены')
    return result


def manage_position(position, context):
    if position is None:
        return {'status': 'НЕТ ДАННЫХ — наличие позиции не указано'}
    if position is False:
        return {'status': 'вне рынка'}
    try:
        side = position['direction']
        if side not in ('LONG', 'SHORT'):
            raise EvidenceError('Некорректное направление позиции')
        sign = 1 if side == 'LONG' else -1
        entry, sl, initial, tp1, tp2, qty, multiplier = [number(position[k], k, True) for k in
            ('entry', 'stop_loss', 'initial_stop_loss', 'tp1', 'tp2', 'quantity', 'contract_multiplier')]
        if sign*(entry-initial) <= 0 or sign*(tp1-entry) <= 0 or sign*(tp2-tp1) <= 0 or sign*(sl-initial) < 0:
            raise EvidenceError('Некорректные уровни позиции или расширенный стоп')
        if not isinstance(position['tp1_filled'], bool):
            raise EvidenceError('Нужен подтверждённый статус исполнения TP1')
        price = context['price']
        action, reason = 'ДЕРЖАТЬ', 'Цена между уровнями; данных об исполнении новых ордеров нет.'
        if sign*(price-sl) <= 0:
            action, reason = 'ЗАКРЫТЬ ПОЛНОСТЬЮ', 'Цена за стопом; фактическое исполнение нужно проверять на бирже.'
        elif sign*(price-tp2) >= 0:
            action, reason = 'ЗАКРЫТЬ ПОЛНОСТЬЮ', 'Цена достигла TP2.'
        elif sign*(price-tp1) >= 0 and not position['tp1_filled']:
            action, reason = 'ЗАКРЫТЬ 50%', 'Цена достигла TP1; закрыть 50% первоначальной позиции.'
        # Trailing requires explicit confirmation from the supplied position evidence.
        new_stop = sl
        lvn = context.get('lvn', [])
        if action == 'ДЕРЖАТЬ' and any(inside(price, z) for z in lvn) and position.get('lvn_entry_confirmed') is True:
            poc = context.get('poc')
            if poc:
                candidate = poc[0] if sign == 1 else poc[1]
                if sign*(candidate-sl) > 0 and sign*(price-candidate) > 0:
                    action, new_stop, reason = 'ПОДТЯНУТЬ СТОП', candidate, 'Подтверждён вход в LVN; текущий POC уменьшает риск.'
        return dict(status='открыта', action=action, reason=reason, stop_loss=new_stop,
                    pnl_gross=sign*(price-entry)*qty*multiplier, poc=context.get('poc'),
                    cvd=context.get('cvd'), structure=context.get('structure'))
    except (KeyError, TypeError, EvidenceError) as exc:
        return {'status': f'НЕТ ДАННЫХ для управления позицией: {exc}'}
