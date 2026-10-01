"""Russian report; missing values are never displayed as zero."""
import json


def render(result):
    c = result['context']
    def show(key):
        value = c.get(key)
        return 'НЕТ ДАННЫХ' if value is None else str(value)
    lines = ['## 1. АНАЛИЗ', '', f'**Актив:** {show("symbol")} ({show("exchange")})',
             f'**Таймфрейм:** {show("timeframe")}', f'**Текущая цена:** {show("price")}',
             f'**Режим рынка:** {c.get("regime", "НЕОПРЕДЕЛЁННЫЙ")}', f'**Структура:** {show("structure")}']
    for name, key in [('VWAP', 'vwap'), ('POC', 'poc'), ('VAH', 'vah'), ('VAL', 'val'),
                      ('HVN', 'hvn'), ('LVN', 'lvn')]:
        lines.append(f'**{name}:** {show(key)}')
    for kind, label in [('OB', 'Order Block'), ('FVG', 'FVG')]:
        found = [z['zone'] for z in c.get('smart_money', []) if z['kind'] == kind]
        lines.append(f'**{label}:** {found or "НЕТ ПОДТВЕРЖДЕНИЯ"}')
    lines += [f'**Liquidity:** {show("liquidity")}', f'**CVD:** {show("cvd")}',
              f'**ATR:** {show("atr")}', '', '## 2. ЧЕК-ЛИСТ', '']
    for i, check in enumerate(result['checks'], 1):
        lines += [f'**{i}. {check["name"]}:** {"✅" if check["passed"] else "❌"}', check['detail'], '']
    for name, f in result['filters'].items():
        lines.append(f'**Фильтр {name}:** {"✅" if f["passed"] else "❌"} {f["detail"]}')
    lines += ['', '## 3. РЕШЕНИЕ', '', '# '+result['decision'], '']
    trade = result['trade']
    if trade:
        for name, key in [('Entry', 'entry'), ('Stop Loss', 'stop_loss'), ('Расстояние до SL', 'stop_distance'),
                          ('Расстояние до SL, % цены', 'stop_percent'), ('TP1 (50%)', 'tp1'),
                          ('TP2 (50%)', 'tp2'), ('RR 50/50', 'rr'), ('Депозит', 'deposit'),
                          ('Допустимый риск (1% депозита)', 'risk_budget'), ('Размер позиции', 'size')]:
            lines.append(f'**{name}:** {trade.get(key) if trade.get(key) is not None else "НЕТ ДАННЫХ"}')
        lines += [trade['size_note'], '**Основание входа:** Все 7 пунктов и дополнительные фильтры подтверждены.']
    else:
        lines += ['**Причина пропуска:**'] + ['- '+x for x in result['reasons']]
        lines += ['- '+x for x in result['missing']]
    lines += ['', '## 4. УПРАВЛЕНИЕ ПОЗИЦИЕЙ', '']
    p = result['position']
    lines += [f'**Статус позиции:** {p["status"]}']
    if p.get('action'):
        for name, key in [('Текущий PnL (без комиссий)', 'pnl_gross'), ('POC', 'poc'), ('CVD', 'cvd'),
                          ('Структура', 'structure'), ('Stop Loss', 'stop_loss'), ('Действие', 'action')]:
            lines.append(f'**{name}:** {p.get(key) if p.get(key) is not None else "НЕТ ДАННЫХ"}')
        lines.append(p['reason'])
    return '\n'.join(lines)
