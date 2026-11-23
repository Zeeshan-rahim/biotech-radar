"""Valuation, metrics, rule checks and sell-limit logic. Mirrors index.html (keep the two in step).

Units in company['fundamentals']: A$ millions for ebit, ebitMid, netDebt, fcf, investedCapital;
millions for shares; A$ per share for eps, nav, book; growth and roc as fractions (0.12 = 12%).
"""
import math
from datetime import date


def years_between(a, b):
    return (b - a).days / 365.25


def parse_date(s):
    try:
        y, m, d = (int(x) for x in str(s)[:10].split('-'))
        return date(y, m, d)
    except Exception:
        return None


def num(v):
    try:
        if v is None or v == '':
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def trial_pos(trial, cfg):
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


def liquidity(history):
    """Average daily turnover (A$) over the last 20 sessions that have volume."""
    rows = [h for h in (history or [])[-30:] if len(h) > 2 and h[2]][-20:]
    if len(rows) < 5:
        return None
    return sum(h[1] * h[2] for h in rows) / len(rows)


def metrics(co, price, history=None):
    f = co.get('fundamentals') or {}
    sh, eps, g = num(f.get('shares')), num(f.get('eps')), num(f.get('growth'))
    out = {}
    if price and sh:
        out['mcap'] = price * sh
    if price and eps and eps > 0:
        out['pe'] = price / eps
        if g and g > 0:
            out['peg'] = out['pe'] / (g * 100)
    if g is not None:
        out['growth'] = g
    nd = num(f.get('netDebt'))
    if 'mcap' in out:
        out['ev'] = out['mcap'] + (nd or 0)
        e, em, fcf = num(f.get('ebit')), num(f.get('ebitMid')), num(f.get('fcf'))
        if e and e > 0:
            out['evEbit'] = out['ev'] / e
        if em and em > 0:
            out['evEbitMid'] = out['ev'] / em
        if fcf is not None and out['mcap'] > 0:
            out['fcfYield'] = fcf / out['mcap']
    roc = num(f.get('roc'))
    if roc is None:
        e, ic = num(f.get('ebit')), num(f.get('investedCapital'))
        if e is not None and ic and ic > 0:
            roc = e / ic
    if roc is not None:
        out['roc'] = roc
    b, nav = num(f.get('book')), num(f.get('nav'))
    if price and b and b > 0:
        out['pb'] = price / b
    if price and nav and nav > 0:
        out['pNav'] = price / nav
    lq = liquidity(history)
    if lq is not None:
        out['liquidity'] = lq
    if (co.get('thesis') or {}).get('trigger'):
        out['trigger'] = True
    return out


def check_gates(co, m, cfg):
    if co.get('sleeve') in ('rare', 'device'):
        return []
    rules = cfg.get('gates', {}).get(co.get('animal'), [])
    pf = cfg.get('portfolio', {})
    res = []
    for key, op, val, label in rules:
        if isinstance(val, str):
            val = pf.get(val)
        v = m.get(key)
        if op == 'present':
            ok = True if v else None
        elif v is None:
            ok = None
        elif op == '<=':
            ok = v <= val
        elif op == '>=':
            ok = v >= val
        elif op == 'between':
            ok = val[0] <= v <= val[1]
        else:
            ok = None
        res.append({'key': key, 'label': label, 'ok': ok, 'value': v})
    return res


def sleeve_rules(co, cfg):
    sl = cfg.get('sleeves', {})
    defaults = {'core': {'label': 'Core ETF / income (outside the 10-stock plan)', 'slots': 0, 'weight': 0, 'targetReturn': 0.07, 'maxYears': 10}}
    return sl.get(co.get('sleeve') or 'watch') or defaults.get(co.get('sleeve')) or sl.get('watch') or {'targetReturn': 0.15, 'maxYears': 3}


def evaluate(co, price, cfg, today=None, history=None):
    today = today or date.today()
    out = {'signal': 'No data', 'price': price}
    sc = [s for s in co.get('scenarios', []) if (num(s.get('prob')) or 0) > 0 and num(s.get('value')) is not None]
    m = metrics(co, price, history)
    gates = check_gates(co, m, cfg)
    out.update({'metrics': m, 'gates': gates})
    if not sc or not price:
        return out
    tot = sum(num(s['prob']) for s in sc)
    ev = sum(num(s['prob']) * num(s['value']) for s in sc) / tot
    hz = parse_date(co.get('horizon')) or today
    T = max(0.25, years_between(today, hz))
    floor = cfg['rules'].get('sellFloorReturn', 0.05)
    sell = ev / (1 + floor) ** T
    succ = [s for s in sc if s.get('success')]
    ps = sum(num(s['prob']) for s in succ)
    sv = (sum(num(s['prob']) * num(s['value']) for s in succ) / ps) if ps else None
    bear = min(num(s['value']) for s in sc)
    r = sleeve_rules(co, cfg)
    if r.get('targetMultiple'):
        mult = r['targetMultiple']
        buy = min(sv / mult if sv else 0, ev)
        rule_txt = f'{mult}x if it works'
    else:
        tr = r.get('targetReturn', 0.15)
        buy = ev / (1 + tr) ** T
        rule_txt = f'{round(tr * 100)}% a year'
    buy = min(buy, sell)
    mb, ms = num(co.get('buyOverride')), num(co.get('sellOverride'))
    if mb and mb > 0:
        buy = mb
    if ms and ms > 0:
        sell = ms
    exp_ret = (ev / price) ** (1 / T) - 1
    fails = [g for g in gates if g['ok'] is False]
    unknown = [g for g in gates if g['ok'] is None]
    if co.get('thesisBroken'):
        sig = 'Sell — thesis broken'
    elif price >= sell:
        sig = 'Sell / trim'
    elif price <= buy:
        sig = 'Cheap — fails checks' if fails else ('Cheap — needs checking' if unknown else 'Buy')
    else:
        sig = 'Hold'
    out.update({'signal': sig, 'ev': ev, 'buy': buy, 'sell': sell, 'T': T, 'expRet': exp_ret, 'psucc': ps / tot,
                'sv': sv, 'upIfWorks': (sv / price) if sv else None, 'downIfFails': bear / price - 1,
                'rule': rule_txt, 'draft': not co.get('reviewed'), 'fails': fails, 'unknown': unknown})
    return out


def tick(p):
    return 0.001 if p < 0.10 else (0.005 if p < 2 else 0.01)


def round_tick(p, up=True):
    t = tick(p)
    n = p / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * t, 3)


def suggest_limit(reason, last, level=None, cfg=None):
    """Suggested limit price for a sell order placed for the next session."""
    if reason == 'thesis':
        pct = (cfg or {}).get('sellRules', {}).get('thesisLimitPct', -2)
        return round_tick(last * (1 + pct / 100.0), up=False), 'near the market: getting out matters more than the last cent'
    return round_tick(max(last, level or last), up=True), 'at or above the last close: no rush, leave it in for a few days'
