"""Valuation and probability logic. Mirrors the functions in index.html (keep the two in step)."""
from datetime import date


def years_between(a, b):
    return (b - a).days / 365.25


def parse_date(s):
    try:
        y, m, d = (int(x) for x in str(s)[:10].split('-'))
        return date(y, m, d)
    except Exception:
        return None


def trial_pos(trial, cfg):
    """Probability the trial succeeds (0-1)."""
    st = trial.get('status')
    if st == 'positive':
        return 1.0
    if st == 'negative':
        return 0.0
    if trial.get('posOverride') not in (None, ''):
        return float(trial['posOverride']) / 100.0
    pc = cfg['pos']
    area = pc['areas'].get(trial.get('area'), pc['areas']['other'])
    p = area['p'] * pc['phaseAdj'].get(trial.get('phase', 'P3'), 1.0)
    p = min(max(p, 0.01), 0.99)
    odds = p / (1 - p)
    for m in trial.get('mods', []):
        if m in pc['mods']:
            odds *= pc['mods'][m]['odds']
    return min(odds / (1 + odds), pc.get('cap', 0.97))


def evaluate(co, price, cfg, today=None):
    """Returns dict with signal and levels. Same maths as evaluate() in index.html."""
    today = today or date.today()
    out = {'signal': 'No data', 'price': price}
    sc = [s for s in co.get('scenarios', []) if (s.get('prob') or 0) > 0 and s.get('value') is not None]
    if not sc or not price:
        return out
    tot = sum(s['prob'] for s in sc)
    ev = sum(s['prob'] * s['value'] for s in sc) / tot
    hz = parse_date(co.get('horizon')) or today
    T = max(0.25, years_between(today, hz))
    rules = cfg['rules']
    floor = rules.get('sellFloorReturn', 0.05)
    sell = ev / (1 + floor) ** T
    succ = [s for s in sc if s.get('success')]
    psucc = sum(s['prob'] for s in succ) / tot if succ else 0
    sv = (sum(s['prob'] * s['value'] for s in succ) / sum(s['prob'] for s in succ)) if succ else None
    bear = min(s['value'] for s in sc)
    sec = co.get('section', 'other')
    if sec == 'rare':
        mult = rules['rare'].get('targetMultiple', 10)
        buy = min(sv / mult if sv else 0, ev)
        rule_txt = f"{mult}x if it works"
    else:
        r = rules.get(sec, rules['other']).get('targetReturn', 0.15)
        buy = ev / (1 + r) ** T
        rule_txt = f"{round(r*100)}%/yr"
    buy = min(buy, sell)
    exp_ret = (ev / price) ** (1 / T) - 1
    if price <= buy:
        sig = 'Buy'
    elif price >= sell:
        sig = 'Sell / trim'
    else:
        sig = 'Hold'
    out.update({'signal': sig, 'ev': ev, 'buy': buy, 'sell': sell, 'T': T, 'expRet': exp_ret,
                'psucc': psucc, 'sv': sv, 'upIfWorks': (sv / price) if sv else None,
                'downIfFails': bear / price - 1, 'rule': rule_txt, 'draft': not co.get('reviewed')})
    return out
