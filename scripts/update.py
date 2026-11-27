#!/usr/bin/env python3
"""Medical catalyst radar - scheduled updater.

Runs on GitHub Actions (free). It:
  1. fetches prices and ASX announcements for every company in data/companies.json
  2. turns new announcements into review items (flag-and-confirm) in data/proposals.json
  3. reads Appendix 4C cash-flow reports and proposes the numbers
  4. measures how each stock moved after price-sensitive announcements (data/reactions.json)
  5. raises alerts (GitHub issue -> email, and optional ntfy phone push)

It never edits companies.json or config.json: those belong to you and the web page.
Only standard library, plus pypdf (optional) for reading 4C PDFs.
"""
import json, os, re, sys, time, urllib.request, urllib.error, io
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from model import evaluate, trial_pos, parse_date, suggest_limit, sleeve_rules, round_tick  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA = os.path.join(ROOT, 'data')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'
MARKIT_TOKEN = '83ff96335c2d45a094df02a206a39ff4'  # public token used by asx.com.au's own pages
try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo('Australia/Sydney')
except Exception:  # pragma: no cover
    SYD = timezone(timedelta(hours=10))

ERRORS = []
NOTES = []
SOURCES = {}


def log(*a):
    print(*a, flush=True)


def load(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save(name, obj):
    with open(os.path.join(DATA, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def http(url, binary=False, headers=None, data=None, method=None, timeout=25, tries=2):
    h = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h, data=data, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return b if binary else b.decode('utf-8', 'replace')
        except Exception as e:  # network errors are expected sometimes
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- prices
def _last_trading_day():
    """Date the latest close belongs to. Before ~10:15 am Sydney the last price is the previous session's close."""
    now = datetime.now(SYD)
    d = now.date()
    if now.hour * 60 + now.minute < 10 * 60 + 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _yahoo(ticker):
    for host in ('query2', 'query1'):
        try:
            url = f'https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}.AX?range=2y&interval=1d'
            j = json.loads(http(url, tries=1))
            res = j['chart']['result'][0]
            out = []
            q = res['indicators']['quote'][0]
            vols = q.get('volume') or [None] * len(q['close'])
            for t, c, v in zip(res['timestamp'], q['close'], vols):
                if c is None:
                    continue
                d = datetime.fromtimestamp(t, SYD).date().isoformat()
                row = [d, round(c, 4), int(v) if v else None]
                if out and out[-1][0] == d:
                    out[-1] = row
                else:
                    out.append(row)
            if out:
                return out
        except Exception as e:
            last = e
    raise last


def _stooq(ticker):
    txt = http(f'https://stooq.com/q/d/l/?s={ticker.lower()}.au&i=d', headers={'Accept': 'text/csv'}, tries=1)
    out = []
    for line in txt.splitlines()[1:]:
        p = line.split(',')
        if len(p) >= 5 and re.match(r'\d{4}-\d\d-\d\d', p[0]):
            try:
                vol = int(float(p[5])) if len(p) > 5 and p[5].strip() else None
                out.append([p[0], round(float(p[4]), 4), vol])
            except ValueError:
                pass
    if not out:
        raise ValueError('no rows')
    return out[-520:]


def fetch_prices(ticker):
    """Daily closes. Tries Yahoo, then Stooq, then the ASX site's last price.
    The last-price route still builds a history over time because each run's price is kept."""
    fails = []
    for name, fn in (('Yahoo Finance', _yahoo), ('Stooq', _stooq)):
        try:
            out = fn(ticker)
            SOURCES.setdefault(ticker, {})['price'] = name
            return out
        except Exception as e:
            fails.append(f'{name}: {str(e)[:60]}')
    try:
        j = json.loads(http(f'https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/header'))
        d = j.get('data', j)
        px = None
        for k in ('priceLast', 'lastPrice', 'price', 'last'):
            if isinstance(d, dict) and d.get(k) not in (None, ''):
                px = float(d[k]); break
        if px:
            SOURCES.setdefault(ticker, {})['price'] = 'ASX last price (history builds up each run)'
            NOTES.append(f'{ticker}: price history sources busy ({"; ".join(fails)}) - used ASX last price')
            vol = None
            for k in ('volume', 'volumeToday', 'tradedVolume'):
                if isinstance(d, dict) and d.get(k) not in (None, ''):
                    try:
                        vol = int(float(d[k])); break
                    except (TypeError, ValueError):
                        pass
            return [[_last_trading_day(), px, vol]]
    except Exception as e:
        fails.append(f'ASX: {e}')
    ERRORS.append(f'{ticker}: no price today ({"; ".join(fails)})')
    return []


# ---------------------------------------------------------------- announcements
def _iso(s):
    """Parse many date formats to an aware datetime in Sydney time."""
    if not s:
        return None
    s = str(s).strip().replace('Z', '+00:00')
    s = re.sub(r'\.\d+(?=[+-]|$)', '', s)
    s = re.sub(r'([+-]\d\d)(\d\d)$', r'\1:\2', s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], '%Y-%m-%d')
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SYD)
    return dt.astimezone(SYD)


def fetch_announcements(ticker):
    items = []
    try:
        url = f'https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements?count=40&market_sensitive=false'
        j = json.loads(http(url))
        raw = (j.get('data') or {}).get('items') or []
        for a in raw:
            key = a.get('documentKey') or a.get('id')
            dt = _iso(a.get('date') or a.get('releaseDate'))
            if not key or not dt:
                continue
            items.append({'id': str(key), 'at': dt.isoformat(), 'date': dt.date().isoformat(),
                          'headline': (a.get('headline') or '').strip(),
                          'sensitive': bool(a.get('isPriceSensitive') or a.get('marketSensitive')),
                          'url': f'https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/{key}?access_token={MARKIT_TOKEN}',
                          'pages': a.get('fileSize') or ''})
        if items:
            SOURCES.setdefault(ticker, {})['announcements'] = 'ASX (Markit)'
            return items
    except Exception as e:
        ERRORS.append(f'{ticker}: ASX announcements failed ({e})')
    try:
        j = json.loads(http(f'https://www.asx.com.au/asx/1/company/{ticker}/announcements?count=20&market_sensitive=false'))
        for a in j.get('data', []):
            dt = _iso(a.get('document_release_date') or a.get('document_date'))
            if not dt:
                continue
            items.append({'id': str(a.get('id')), 'at': dt.isoformat(), 'date': dt.date().isoformat(),
                          'headline': (a.get('header') or '').strip(), 'sensitive': bool(a.get('market_sensitive')),
                          'url': a.get('url', ''), 'pages': a.get('number_of_pages', '')})
        if items:
            SOURCES.setdefault(ticker, {})['announcements'] = 'ASX (legacy API)'
    except Exception as e:
        ERRORS.append(f'{ticker}: ASX legacy announcements failed ({e})')
    return items


# ---------------------------------------------------------------- 4C parsing
NUM = r'\(?-?\$?[\d,]+(?:\.\d+)?\)?'


def _num(tok):
    neg = tok.startswith('(') or tok.startswith('-')
    v = float(re.sub(r'[^\d.]', '', tok) or 0)
    return -v if neg else v


def _after(text, label_re, n=1):
    m = re.search(label_re + r'[^\d(\-]{0,90}?(' + NUM + r')(?:\s+(' + NUM + r'))?', text, re.I | re.S)
    if not m:
        return None
    return _num(m.group(1))


MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}


def parse_4c(text):
    """Pull the main lines from an ASX Appendix 4C. Values returned in A$ millions."""
    t = re.sub(r'[ \t ]+', ' ', text)
    out = {}
    m = re.search(r'quarter ended[^0-9]{0,40}(\d{1,2})\s+([A-Za-z]+)\s+(20\d\d)', t, re.I)
    if m and m.group(2).lower() in MONTHS:
        out['period'] = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    fields = {
        'opcf': r'net cash from\s*/?\s*\(?used in\)?\s*operating\s+activities',
        'cash': r'cash and cash equivalents at end of (?:the )?(?:period|quarter)',
        'rnd': r'research and development',
        'staff': r'staff costs',
        'admin': r'administration and corporate costs',
        'receipts': r'receipts from customers',
        'interest': r'interest received',
        'grants': r'government grants and tax incentives',
    }
    for k, rx in fields.items():
        v = _after(t, rx)
        if v is not None:
            out[k] = round(v / 1000.0, 3)  # $A'000 -> $m
    q = re.search(r'estimated quarters of funding available\s*(?:\([^)]*\))?[^\d(]{0,60}?(\d+(?:\.\d+)?)', t, re.I | re.S)
    if q:
        out['quartersFunded'] = float(q.group(1))
    return out


def read_pdf_text(url):
    try:
        import pypdf
    except ImportError:
        ERRORS.append('pypdf not installed - 4C numbers not read')
        return ''
    b = http(url, binary=True, timeout=40)
    r = pypdf.PdfReader(io.BytesIO(b))
    return '\n'.join((p.extract_text() or '') for p in r.pages[:12])


# ---------------------------------------------------------------- classify
STOP = set('with from that this into after before their about phase trial results result update company program study first second third final annual general meeting quarter report'.split())
RX = {
    'halt': r'trading halt|suspension from (?:official )?quotation|voluntary suspension|pause in trading',
    'finance': r'appendix 4c|quarterly activit|quarterly cash ?flow',
    'report': r'appendix 4d|appendix 4e|half.?year(?:ly)? (?:report|results|accounts)|annual report|preliminary final|full.?year results',
    'raise': r'placement|capital rais|entitlement|share purchase plan|\bspp\b|proposed issue of securities|institutional offer|bookbuild|convertible note',
    'admin': r'application for quotation|cleansing|appendix 2a|appendix 3[bgy]|change of director|director.s interest|notice of (?:annual )?general meeting|proxy form|becoming a substantial|ceasing to be a substantial|change in substantial|results of meeting|constitution|corporate governance',
    'regulatory': r'\bfda\b|\bema\b|chmp|tga|approv|designation|orphan|breakthrough|fast track|clearance|de novo|510\(k\)|\bind\b|\bnda\b|\bcrl\b|complete response|priority review|voucher',
    'deal': r'licen[cs]|partner|agreement|collaboration|acquisition|acquire|milestone payment|royalt',
    'trialword': r'top.?line|results|data|primary endpoint|readout|enrol|recruit|dosed|interim|analysis|study|trial|phase',
}
POS_WORDS = r'met (?:its |the |both )?(?:primary|co-primary)|positive|successful|statistically significant|achieved|consistent with|exceed'
NEG_WORDS = r'did not meet|not meet|failed|fail to|discontinu|futility|terminat|negative|missed|halt(?:ed)? the|suspend'


def classify(ann, co):
    h = ann['headline'].lower()
    keys = [k.strip() for k in (co.get('keywords') or '').lower().split('|') if k.strip()]
    for t in co.get('trials', []):
        keys += [k.strip() for k in (t.get('keys') or '').lower().split('|') if k.strip()]
    keyhit = [k for k in keys if k and k in h]
    patches = []
    kind = None
    if re.search(RX['halt'], h):
        kind = 'halt'
    elif re.search(RX['finance'], h):
        kind = 'finance'
    elif re.search(RX['report'], h):
        kind = 'report'
    elif re.search(RX['raise'], h):
        kind = 'raise'
        patches.append({'op': 'addRaise', 'entry': {'date': ann['date'], 'amount': None, 'price': None}})
    elif re.search(RX['admin'], h) and not ann['sensitive']:
        return None
    elif keyhit and re.search(RX['trialword'], h):
        kind = 'trial'
    elif re.search(RX['regulatory'], h):
        kind = 'regulatory'
    elif re.search(RX['deal'], h):
        kind = 'deal'
    elif ann['sensitive']:
        kind = 'info'
    else:
        return None
    sentiment = 'positive' if re.search(POS_WORDS, h) else ('negative' if re.search(NEG_WORDS, h) else 'unclear')
    # trial status suggestion
    if kind in ('trial', 'regulatory'):
        for t in co.get('trials', []):
            tk = [k.strip() for k in (t.get('keys') or '').lower().split('|') if k.strip()]
            if any(k in h for k in tk) and re.search(r'top.?line|results|primary endpoint|readout', h):
                patches.append({'op': 'trialStatus', 'trialId': t['id'], 'status': sentiment if sentiment != 'unclear' else 'check',
                                'text': ann['headline']})
    # catalyst match
    ad = parse_date(ann['date'])
    for c in co.get('catalysts', []):
        if c.get('done'):
            continue
        words = [w for w in re.findall(r'[a-z0-9\-]{4,}', c['title'].lower()) if w not in STOP]
        hits = [w for w in words if w in h]
        cd = parse_date(c.get('date'))
        near = cd and ad and abs((cd - ad).days) <= 240
        if near and (len(hits) >= 2 or (hits and any(h2 in keys for h2 in hits))):
            patches.append({'op': 'catalystDone', 'catalystId': c['id'], 'title': c['title'],
                            'result': ann['headline']})
    patches.append({'op': 'addHistory', 'entry': {'date': ann['date'], 'title': ann['headline'][:120], 'desc': ''}})
    return {'kind': kind, 'sentiment': sentiment, 'patches': patches}


# ---------------------------------------------------------------- reactions
def reaction(ann, px):
    """% move from last close before release to close on the reaction day, plus 5 trading days."""
    if not px:
        return None
    at = _iso(ann['at'])
    d = at.date().isoformat()
    after_close = at.hour * 60 + at.minute >= 16 * 60 + 10
    before = [p for p in px if p[0] < d]
    on = [p for p in px if (p[0] > d if after_close else p[0] >= d)]
    if not before or not on:
        return None
    b = before[-1][1]
    r1 = on[0][1] / b - 1
    r5 = on[min(4, len(on) - 1)][1] / b - 1 if len(on) >= 2 else None
    return {'base': b, 'day': on[0][0], 'd1': round(r1 * 100, 1), 'd5': round(r5 * 100, 1) if r5 is not None else None,
            'complete5': len(on) >= 5}


# ---------------------------------------------------------------- recurring catalysts
def expand_recurring(co, today, months=12):
    out = []
    end = today + timedelta(days=31 * months)
    rec = list(co.get('recurring') or [])
    if co.get('reporting') == 'quarterly':
        rec.append({'label': 'Quarterly 4C', 'months': [1, 4, 7, 10], 'day': 31, 'title': 'Quarterly activities report and Appendix 4C', 'event': 'report'})
    for r in rec:
        for y in (today.year, today.year + 1, today.year + 2):
            for m in r['months']:
                d = min(r.get('day', 28), [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
                dt = date(y, m, d)
                if today - timedelta(days=3) <= dt <= end:
                    out.append({'id': f"rec-{co['id']}-{r['label']}-{dt.isoformat()}", 'date': dt.isoformat(), 'title': r['title'],
                                'big': False, 'event': r.get('event', 'report'), 'done': False, 'recurring': True})
    return out


# ---------------------------------------------------------------- alerts
def latest_finance(co):
    f = [x for x in co.get('finance', []) if x.get('cash') is not None]
    return sorted(f, key=lambda x: x.get('period', ''))[-1] if f else None


def funding_gap(co, today):
    """Will cash last to the next big catalyst? Mirrors fundingGap() in index.html."""
    f = latest_finance(co)
    if not f or f.get('profitable'):
        return None
    burn = co.get('burnQ')
    if burn in (None, ''):
        burn = -f['opcf'] if f.get('opcf') is not None and f['opcf'] < 0 else None
    if not burn:
        return None
    burn = float(burn)
    tgt = None
    ft = co.get('fundTo') or {}
    if ft.get('date') and parse_date(ft['date']):
        tgt = (ft.get('label') or 'target', parse_date(ft['date']))
    else:
        cs = sorted([c for c in co.get('catalysts', []) if c.get('big') and not c.get('done') and parse_date(c.get('date'))
                     and parse_date(c.get('date')) >= today], key=lambda c: c['date'])
        if cs:
            tgt = (cs[0].get('title', 'catalyst'), parse_date(cs[0]['date']))
    if not tgt:
        return None
    start = parse_date(f.get('period')) or today
    q = max(0.0, (tgt[1] - start).days / 91.31)
    need = burn * q + float(co.get('extraSpend') or 0)
    left = float(f['cash']) - need
    status = 'short' if left < 0 else ('tight' if left / burn < 2 else 'ok')
    return {'status': status, 'label': tgt[0], 'date': tgt[1].isoformat(), 'left': round(left, 1), 'cash': f['cash'], 'burn': burn}


def raise_window(co, today):
    """Python mirror of the site's raise forecast: actual 4C cash, then your quarterly forecasts, then flat burn."""
    def qend(d):
        m = ((d.month - 1) // 3 + 1) * 3
        nxt = date(d.year + (m == 12), 1 if m == 12 else m + 1, 1)
        return nxt - timedelta(days=1)
    def lab(d):
        return ['Mar', 'Jun', 'Sep', 'Dec'][(d.month - 1) // 3] + '-' + str(d.year)[2:]
    rows = {}
    for f in co.get('finance') or []:
        d = parse_date(f.get('period'))
        if d and f.get('cash') is not None:
            rows[qend(d)] = {'cash': float(f['cash']), 'opcf': f.get('opcf'), 'kind': 'a'}
    for f in co.get('forecasts') or []:
        d = parse_date(f.get('period'))
        if d and f.get('cash') is not None and rows.get(qend(d), {}).get('kind') != 'a':
            rows[qend(d)] = {'cash': float(f['cash']), 'opcf': f.get('opcf'), 'kind': 'f'}
    if not rows:
        return None
    ks = sorted(rows)
    last = ks[-1]; r = rows[last]
    burns = [rows[k]['opcf'] for k in ks if rows[k].get('opcf') is not None]
    opcf = rows[last]['opcf'] if rows[last]['kind'] == 'f' and rows[last].get('opcf') is not None else (sum(burns[-4:]) / len(burns[-4:]) if burns else None)
    if co.get('burnQ') not in (None, '') and rows[last]['kind'] == 'a':
        opcf = -float(co['burnQ'])
    horizon = qend(today + timedelta(days=730))
    cur = last; cash = r['cash']
    while opcf is not None and opcf < 0 and cur < horizon:
        cur = qend(cur + timedelta(days=5)); cash += opcf
        rows[cur] = {'cash': cash, 'opcf': opcf, 'kind': 'auto'}
    ks = sorted(rows)
    out = {}
    for i, k in enumerate(ks):
        if k < qend(today) and rows[k]['kind'] == 'a':
            continue
        nb = rows[ks[i + 1]].get('opcf') if i + 1 < len(ks) else rows[k].get('opcf')
        nb = -nb if nb is not None and nb < 0 else None
        if not nb:
            continue
        ql = rows[k]['cash'] / nb
        if 'open' not in out and ql < 3:
            out.update(open=k.isoformat(), open_label=lab(k), cash=round(rows[k]['cash'], 1), report=(k + timedelta(days=30)).isoformat())
        if 'must_label' not in out and ql < 2:
            out['must_label'] = lab(k)
    if 'open' not in out or parse_date(out['open']) > today + timedelta(days=280):
        return None   # only alert when the window opens within ~3 quarters
    return out


# ------------------------------------------------------------------ automatic rules twin
def stake_fee(value):
    """CommSec online brokerage (matches the user's contract notes)."""
    return 10.0 if value <= 1000 else 19.95 if value <= 10000 else 29.95 if value <= 25000 else round(value * 0.0012, 2)


def twin_step(cfg, companies, prices, twin, today):
    """Trade the A$500k plan strictly by the rules, at the latest close. Returns list of alert dicts.
    Buys: signal Buy + research reviewed + plan sleeve + free slot + caps; rare/insect wait if a raise window opens within ~2 quarters.
    Sells: thesis broken, at/above sell price, or holding limit reached."""
    pf = cfg.get('portfolio') or {}
    sleeves = cfg.get('sleeves') or {}
    bm = pf.get('benchmark')
    last = lambda t: ((prices.get(t, {}).get('history') or [[None, None]])[-1])
    if not twin.get('start'):
        cap = float(pf.get('paperCapital', 500000))
        twin.update({'capital': cap, 'cash': cap, 'start': today.isoformat(), 'holdings': {}, 'trades': [], 'history': [],
                     'benchStart': last(bm)[1] if bm else None,
                     'rules': 'Buy at the close when a reviewed stock signals Buy (sized by its sleeve weight, slot/2–18%/Health Care 30% limits; rare & Insects wait for the raise). Sell at the close on thesis broken, sell price reached, or holding limit. CommSec brokerage charged.'})
    day = max((last(c['ticker'])[0] or '') for c in companies) if companies else ''
    if not day or day <= (twin.get('lastDay') or ''):
        return []
    twin['lastDay'] = day
    byT = {c['ticker']: c for c in companies}
    out = []
    H = twin['holdings']

    def value():
        return twin['cash'] + sum(h['shares'] * (last(t)[1] or h['cost']) for t, h in H.items())

    def trade(t, side, sh, px, reason):
        amt = sh * px
        fee = stake_fee(amt)
        if side == 'buy':
            twin['cash'] -= amt + fee
            H[t] = {'shares': sh, 'cost': (amt + fee) / sh, 'date': today.isoformat(), 'sleeve': byT[t].get('sleeve')}
        else:
            h = H.pop(t)
            twin['cash'] += amt - fee
            gain = amt - fee - h['shares'] * h['cost']
            reason += f" (gain A${gain:,.0f})"
        twin['trades'].append({'date': today.isoformat(), 'priceDate': last(t)[0], 't': t, 'side': side, 'shares': sh, 'price': px, 'fee': fee, 'reason': reason})
        out.append({'key': f'twin:{t}:{side}:{today}', 'level': 'info',
                    'text': f"🤖 **Rules twin {side.upper()} {t}**: {sh:,} @ A${px:.3f} — {reason}. Did you do the same?"})

    # sells
    for t in list(H):
        co = byT.get(t)
        px = last(t)[1]
        if not co or not px:
            continue
        ev = evaluate(co, px, cfg, today, prices.get(t, {}).get('history'))
        maxy = sleeve_rules(co, cfg).get('maxYears', 3)
        held_days = (today - parse_date(H[t]['date'])).days
        if ev['signal'] == 'Sell — thesis broken':
            trade(t, 'sell', H[t]['shares'], px, 'thesis marked broken')
        elif ev['signal'] == 'Sell / trim':
            trade(t, 'sell', H[t]['shares'], px, f"price reached the sell price A${ev['sell']:.3f}")
        elif held_days >= maxy * 365.25:
            trade(t, 'sell', H[t]['shares'], px, f'{maxy:g}-year holding limit reached')
    # buys
    total = value()
    health = sum(h['shares'] * (last(t)[1] or 0) for t, h in H.items() if (byT.get(t) or {}).get('sector') == 'health')
    cands = []
    for co in companies:
        t = co['ticker']
        sl = co.get('sleeve') or 'watch'
        rules = sleeves.get(sl) or {}
        px = last(t)[1]
        if t in H or not px or not co.get('reviewed') or sl in ('watch', 'core') or not rules.get('slots'):
            continue
        if sum(1 for h in H.values() if h.get('sleeve') == sl) >= rules['slots']:
            continue
        ev = evaluate(co, px, cfg, today, prices.get(t, {}).get('history'))
        if ev['signal'] != 'Buy':
            continue
        if (sl in ('rare', 'insect') or co.get('animal') == 'insect'):
            rw = raise_window(co, today)
            if rw and parse_date(rw['open']) <= today + timedelta(days=200):
                continue
        cands.append((ev.get('expRet') or 0, co, px, rules))
    for _, co, px, rules in sorted(cands, key=lambda x: -x[0]):
        t = co['ticker']
        w = min(max(float(rules.get('weight', 0)), pf.get('minWeight', 2)), pf.get('maxWeight', 18)) / 100
        amt = total * w
        if co.get('sector') == 'health':
            amt = min(amt, total * pf.get('healthCap', 30) / 100 - health)
        amt = min(amt, twin['cash'] - 50)   # weights leave ~6% cash by design; never go negative
        sh = int(amt // px)
        if sh <= 0 or sh * px < total * pf.get('minWeight', 2) / 100 * 0.5:
            continue
        trade(t, 'buy', sh, px, f"signal Buy at A${px:.3f} ({co.get('animal','')}, {co.get('sleeve')} sleeve, {w*100:g}% weight)")
        if co.get('sector') == 'health':
            health += sh * px
    # history point
    bpx = last(bm)[1] if bm else None
    hist = twin['history']
    pt = [day, round(value(), 2), round(twin['capital'] * bpx / twin['benchStart'], 2) if bpx and twin.get('benchStart') else None]
    if hist and hist[-1][0] == day:
        hist[-1] = pt
    else:
        hist.append(pt)
    twin['history'] = hist[-1500:]
    twin['trades'] = twin['trades'][-1000:]
    return out


def build_alerts(cfg, companies, prices, positions, new_anns, new_props, state, today, first_run, paper=None):
    paper = paper or {}
    A = cfg.get('alerts', {})
    out = []

    def fire(key, val, text, level='info'):
        if state.get(key) == val:
            return
        state[key] = val
        out.append({'key': key, 'text': text, 'level': level})

    for co in companies:
        t = co['ticker']
        px = prices.get(t, {}).get('history') or []
        last = px[-1][1] if px else None
        # levels and sell rules
        held_real = bool(positions.get(t, {}).get('held') or positions.get(t, {}).get('shares'))
        held_paper = t in (paper.get('holdings') or {})
        wa = co.get('alerts') or {}
        watch = bool(wa.get('watch'))
        held = held_real or held_paper or watch
        who = 'your holding' if held_real else ('paper portfolio' if held_paper else ('watch alert' if watch else ''))
        if last:
            ev = evaluate(co, last, cfg, today, px)
            tag = ' (draft assumptions — review them)' if ev.get('draft') else ''
            sig = ev['signal']
            if sig == 'Sell — thesis broken' and held:
                lim, why = suggest_limit('thesis', last, None, cfg)
                fire(f'zone:{t}', 'Thesis', f"**SELL {t}** ({who}): thesis marked broken. Place a sell limit at **A${lim:g}** for the next session ({why}). Last close A${last:.3f}.", 'action')
            elif sig == 'Sell / trim':
                if held:
                    lim, why = suggest_limit('over', last, ev['sell'], cfg)
                    fire(f'zone:{t}', 'Sell', f"**SELL / TRIM {t}** ({who}): A${last:.3f} is at or above your sell price A${ev['sell']:.3f}{tag}. Suggested sell limit **A${lim:g}** ({why}).", 'action')
                else:
                    fire(f'zone:{t}', 'Sell', f"**{t}** is above its sell price A${ev['sell']:.3f} — not one to buy now{tag}.")
            elif sig == 'Hold' and held and ev.get('sell') and last >= ev['sell'] * (1 - A.get('nearSellPct', 5) / 100):
                gap = (ev['sell'] / last - 1) * 100
                fire(f'near:{t}', 1, f"**GET READY {t}** ({who}): A${last:.3f} is within {gap:.1f}% of your sell price A${ev['sell']:.3f}{tag}. If it closes at or above it you'll get a SELL email with a limit price; you can also pre-set a limit sell at **A${round_tick(ev['sell']):g}** on CommSec now.", 'action')
            elif sig == 'Buy':
                chk = f"passes the {co.get('animal','')} checks" if ev['gates'] else 'speculative sleeve: judged on the scenarios and success odds'
                fire(f'zone:{t}', 'Buy', f"**BUY ZONE {t}**: A${last:.3f} is at or below your buy price A${ev['buy']:.3f} ({chk}){tag}.", 'action')
            elif sig == 'Cheap — needs checking':
                fire(f'zone:{t}', 'Unchecked', f"**{t}** is below your buy price A${ev['buy']:.3f}, but these checks have no data yet: {', '.join(g['label'] for g in ev['unknown'])}. Research before buying.")
            elif sig == 'Cheap — fails checks':
                fire(f'zone:{t}', 'Cheap', f"**{t}** is below your buy price A${ev['buy']:.3f} but fails: {', '.join(g['label'] for g in ev['fails'])}.")
            if sig != 'Hold' or not ev.get('sell') or last < ev['sell'] * (1 - A.get('nearSellPct', 5) / 100) * 0.98:
                state.pop(f'near:{t}', None)
            if sig == 'Hold' and (state.get(f'zone:{t}') in ('Buy', 'Sell', 'Thesis', 'Cheap', 'Unchecked') or first_run):
                fire(f'zone:{t}', 'Hold', f"**{t}** is in the hold zone (buy ≤ A${ev['buy']:.3f}, sell ≥ A${ev['sell']:.3f}).")
        # big move
            if len(px) >= 2:
                mv = (px[-1][1] / px[-2][1] - 1) * 100
                if abs(mv) >= A.get('bigMovePct', 10):
                    fire(f'move:{t}:{px[-1][0]}', 1, f"**{t}** moved {mv:+.1f}% on {px[-1][0]} (A${px[-2][1]:.3f} → A${px[-1][1]:.3f}).", 'action')
        # position ladder and time limit
        pos = positions.get(t) or {}
        plan = pos.get('plan') or wa.get('ladder') or []
        if last and plan:
            for i, tr in enumerate(plan):
                if not tr.get('done') and tr.get('price') and float(tr['price']) * (1 - A.get('nearSellPct', 5) / 100) <= last < float(tr['price']):
                    fire(f'ladnear:{t}:{i}', 1, f"**GET READY {t}**: A${last:.3f} is within {(float(tr['price'])/last-1)*100:.1f}% of sell-ladder step '{tr.get('label','')}' (A${float(tr['price']):.2f}). Consider pre-setting a limit sell at A${float(tr['price']):.2f} on CommSec.", 'action')
                if not tr.get('done') and tr.get('price') and last >= float(tr['price']):
                    amt = f"{tr['shares']:,} shares" if tr.get('shares') else 'your planned amount'
                    lim, why = suggest_limit('ladder', last, float(tr['price']), cfg)
                    if held_real or held_paper:
                        fire(f'ladder:{t}:{i}', 1, f"**SELL {t}** — sell-ladder step '{tr.get('label','')}' reached (≥ A${float(tr['price']):.2f}). Sell {amt} with a limit at **A${lim:g}** ({why}).", 'action')
                    else:
                        fire(f'ladder:{t}:{i}', 1, f"**{t} reached exit level** '{tr.get('label','')}' (≥ A${float(tr['price']):.2f}; watch alert). If you hold it outside the site, sell {amt} with a limit at **A${lim:g}** ({why}).", 'action')
        # watch-alert buy levels (entry plan)
        if last:
            for i, lv in enumerate(wa.get('buyBelow') or []):
                lp = float(lv['price'])
                if last <= lp:
                    fire(f'buylvl:{t}:{i}', 'hit', f"**BUY LEVEL {t}**: A${last:.3f} is at or below your entry level '{lv.get('label','')}' (A${lp:.3f}). Check the latest news and the research memo before placing a limit buy at or below A${round_tick(lp, up=False):g}.", 'action')
                elif last <= lp * (1 + A.get('nearSellPct', 5) / 100):
                    fire(f'buylvl:{t}:{i}', 'near', f"**GET READY {t}**: A${last:.3f} is within {(last/lp-1)*100:.1f}% of your entry level '{lv.get('label','')}' (A${lp:.3f}).", 'info')
                elif last > lp * 1.15:
                    state.pop(f'buylvl:{t}:{i}', None)
        # raise window from quarterly forecasts (rare disease / insects / watched)
        if held or co.get('sleeve') in ('rare', 'insect') or co.get('animal') == 'insect':
            rw = raise_window(co, today)
            if rw:
                fire(f'raise:{t}', rw['open'], f"**RAISE AHEAD {t}**{' (' + who + ')' if who else ''}: cash A${rw['cash']:g}m falls under 3 quarters of burn in the **{rw['open_label']}** quarter (report ~{rw['report']})" + (f"; very likely by {rw['must_label']}" if rw.get('must_label') else '') + ". " + ("You hold it: plan to trim before and rebuy at the raise." if held_real else "Your rule: the raise is the entry — wait for the placement price."), 'action')
        # funding gap (raise before catalyst) - your spreadsheet rule
        if held:
            g = funding_gap(co, today)
            if g and g['status'] != 'ok':
                txt = (f"**{t}** ({who}): cash A${g['cash']}m at ~A${g['burn']:g}m a quarter may not last to {g['label']} ({g['date']})"
                       + (f" — short by about A${-g['left']:g}m. **Capital raise likely**: consider trimming before it and buying back at the raise." if g['status'] == 'short'
                          else " — under 2 quarters spare, a raise around the catalyst is likely."))
                fire(f'fund:{t}', g['status'], txt, 'action' if g['status'] == 'short' else 'info')
        bought = pos.get('bought') or (paper.get('holdings') or {}).get(t, {}).get('date')
        if bought:
            b = parse_date(bought)
            maxy = sleeve_rules(co, cfg).get('maxYears', 3)
            if b:
                end = b + timedelta(days=int(maxy * 365.25))
                left = (end - today).days
                for d in sorted(A.get('timeWarnDays', [180, 90, 30])):
                    if left <= d:
                        lim, why = suggest_limit('time', last, None, cfg) if last else (None, '')
                        msg = (f"**{t}** ({who}): your {maxy:g}-year holding limit ends **{end.isoformat()}** — {max(left,0)} days left. "
                               + ("Time to sell or re-justify the thesis." if left <= 30 else "Start planning the exit: re-check the thesis and sell price.")
                               + (f" Suggested limit if selling: A${lim:g} ({why})." if lim and left <= 30 else ''))
                        fire(f'time:{t}', d, msg, 'action' if left <= 90 else 'info')
                        break
        # catalysts
        for c in co.get('catalysts', []) + expand_recurring(co, today, 2):
            if c.get('done'):
                continue
            cd = parse_date(c.get('date'))
            if not cd:
                continue
            days = (cd - today).days
            if 0 <= days <= A.get('catalystDaysAhead', 7):
                fire(f"soon:{c['id']}", 1, f"**{t}** catalyst in {days} day(s): {c['title']} ({c.get('when') or cd.isoformat()}).", 'action' if c.get('big') else 'info')
            if not c.get('recurring') and days < -A.get('overdueGraceDays', 3):
                fire(f"overdue:{c['id']}", 1, f"**{t}** catalyst date has passed without being marked done: {c['title']} (was {c.get('when') or cd}). Check for news or a delay.", 'action' if c.get('big') else 'info')
        # promises
        for p in co.get('promises', []):
            d = parse_date(p.get('due'))
            if d and p.get('status') in ('open', 'ontrack') and (today - d).days > A.get('overdueGraceDays', 3):
                fire(f"promise:{p['id']}", 1, f"**{t}** promise past due and not marked kept: “{p['text']}” (due {d}).", 'info')
        # runway
        f = latest_finance(co)
        if f and f.get('opcf') is not None and f['opcf'] < 0 and not f.get('profitable'):
            months = f['cash'] / (-f['opcf']) * 3
            if months < A.get('runwayMonths', 12):
                fire(f"runway:{t}:{f['period']}", 1, f"**{t}** cash runway about {months:.0f} months (cash A${f['cash']}m, burn A${-f['opcf']}m a quarter). A capital raise is likely.", 'action')
    for a in new_anns:
        h = a['headline'].lower()
        if re.search(RX['halt'], h):
            fire(f"halt:{a['id']}", 1, f"**{a['ticker']}** TRADING HALT: {a['headline']} — big news usually follows. [PDF]({a['url']})", 'action')
        elif a['sensitive']:
            fire(f"ann:{a['id']}", 1, f"**{a['ticker']}** price-sensitive: {a['headline']} ({a['date']}). [PDF]({a['url']})", 'info')
    if new_props:
        out.append({'key': 'proposals', 'text': f"{new_props} new item(s) waiting in your Review queue.", 'level': 'info'})
    return out


def deliver(alerts, today):
    if not alerts:
        return
    action = [a for a in alerts if a['level'] == 'action']
    title = f"ZRPL: {len(action)} action, {len(alerts)-len(action)} info — {today.isoformat()}"
    owner = os.environ.get('GITHUB_REPOSITORY_OWNER', '')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    body = '\n'.join(['### Needs a look'] + [f'- {a["text"]}' for a in action] +
                     ['', '### For information'] + [f'- {a["text"]}' for a in alerts if a['level'] != 'action'])
    page = f'https://{owner}.github.io/{repo.split("/")[-1]}/' if owner and repo else ''
    body += f'\n\nOpen the radar: {page}\n\n@{owner}' if owner else ''
    tok = os.environ.get('GITHUB_TOKEN')
    if tok and repo and os.environ.get('RADAR_ISSUES', '1') == '1':
        try:
            http(f'https://api.github.com/repos/{repo}/issues', method='POST',
                 data=json.dumps({'title': title, 'body': body, 'labels': ['radar-alert']}).encode(),
                 headers={'Authorization': f'Bearer {tok}', 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'}, tries=1)
            log('Alert issue created')
        except Exception as e:
            ERRORS.append(f'Could not create alert issue ({e})')
    topic = os.environ.get('NTFY_TOPIC')
    if topic:
        try:
            msg = '\n'.join(re.sub(r'\*\*|\[PDF\]\([^)]*\)', '', a['text']) for a in (action or alerts)[:8])
            http(f'https://ntfy.sh/{topic}', method='POST', data=msg.encode('utf-8'),
                 headers={'Title': title, 'Priority': 'high' if action else 'default', 'Tags': 'pill', 'Click': page}, tries=1)
            log('Phone push sent')
        except Exception as e:
            ERRORS.append(f'ntfy push failed ({e})')


def update_prices(prices, t):
    hist = fetch_prices(t)
    if not hist:
        return
    old = {h[0]: list(h[1:]) for h in prices.get(t, {}).get('history', [])}
    for h in hist:
        prev = old.get(h[0])
        vol = h[2] if len(h) > 2 and h[2] else (prev[1] if prev and len(prev) > 1 else None)
        old[h[0]] = [h[1], vol]
    merged = sorted(old.items())[-800:]
    prices[t] = {'history': [[d, v[0], v[1] if len(v) > 1 else None] for d, v in merged], 'last': merged[-1][1][0],
                 'lastDate': merged[-1][0], 'source': SOURCES.get(t, {}).get('price', '')}


# ---------------------------------------------------------------- main
def main():
    today = datetime.now(SYD).date()
    cfg = load('config.json', {})
    companies = load('companies.json', {'companies': []})['companies']
    for co in companies:  # older files from the Medical catalyst radar
        co.setdefault('sector', 'health')
        co.setdefault('sub', co.get('section', 'other'))
        co.setdefault('sleeve', {'neu': 'rare', 'dxb': 'rare', 'emv': 'device'}.get(co.get('id'), 'watch'))
        co.setdefault('animal', 'insect' if co['sector'] == 'health' else 'monkey')
    prices = load('prices.json', {})
    anns = load('announcements.json', {})
    props = load('proposals.json', {'items': []})
    decisions = load('decisions.json', {})
    reactions = load('reactions.json', {})
    alerts = load('alerts.json', {'state': {}, 'log': []})
    positions = load('positions.json', {})
    paper = load('paper.json', {})
    first_run = not alerts.get('state')
    seen_props = {p['id'] for p in props['items']}
    new_anns, new_props = [], 0

    for co in companies:
        t = co['ticker']
        log(f'== {t}')
        update_prices(prices, t)
        got = fetch_announcements(t)
        known = {a['id'] for a in anns.get(t, [])}
        brand_new = t not in anns
        fresh = [a for a in got if a['id'] not in known]
        for a in fresh:
            a['ticker'] = t
        anns[t] = sorted(anns.get(t, []) + fresh, key=lambda a: a['at'], reverse=True)[:150]
        for a in fresh:
            age = (today - parse_date(a['date'])).days
            if brand_new and age > 60:
                continue
            if not brand_new or age <= 3:
                new_anns.append(a)
            cl = classify(a, co)
            pid = f"{t}:{a['id']}"
            if not cl or pid in seen_props:
                continue
            item = {'id': pid, 'ticker': t, 'company': co['id'], 'date': a['date'], 'at': a['at'], 'headline': a['headline'],
                    'url': a['url'], 'sensitive': a['sensitive'], 'kind': cl['kind'], 'sentiment': cl['sentiment'],
                    'patches': cl['patches'], 'created': today.isoformat()}
            if cl['kind'] == 'finance':
                try:
                    fin = parse_4c(read_pdf_text(a['url']))
                    if fin.get('cash') is not None:
                        fin.setdefault('period', a['date'])
                        fin['source'] = a['headline'][:80]
                        item['patches'].insert(0, {'op': 'addFinance', 'entry': fin})
                        item['parsed'] = fin
                except Exception as e:
                    ERRORS.append(f'{t}: could not read 4C PDF ({e})')
            props['items'].append(item)
            seen_props.add(pid)
            new_props += 1
        time.sleep(0.6)

    # reactions to price-sensitive announcements
    for t, lst in anns.items():
        px = prices.get(t, {}).get('history') or []
        rr = reactions.setdefault(t, {})
        for a in lst:
            if not a.get('sensitive'):
                continue
            if a['id'] in rr and rr[a['id']].get('complete5'):
                continue
            r = reaction(a, px)
            if r:
                kind = (classify(a, next((c for c in companies if c['ticker'] == t), {})) or {}).get('kind', 'info')
                r.update({'headline': a['headline'], 'date': a['date'], 'kind': kind})
                rr[a['id']] = r
    # proposals: attach reactions, drop decided ones older than 90 days, cap size
    for p in props['items']:
        r = reactions.get(p['ticker'], {}).get(p['id'].split(':', 1)[1])
        if r:
            p['reaction'] = {'d1': r['d1'], 'd5': r['d5']}
    cutoff = (today - timedelta(days=90)).isoformat()
    props['items'] = [p for p in props['items'] if not (p['id'] in decisions and p['created'] < cutoff)][-400:]

    pending = sum(1 for p in props['items'] if p['id'] not in decisions)
    bm = (cfg.get('portfolio') or {}).get('benchmark')
    if bm:
        update_prices(prices, bm)
    al = build_alerts(cfg, companies, prices, positions, new_anns, new_props, alerts['state'], today, first_run, paper)
    twin = load('twin.json', {})
    try:
        al += twin_step(cfg, companies, prices, twin, today)
    except Exception as e:
        ERRORS.append(f'rules twin: {e}')
    if first_run and al:
        al.insert(0, {'key': 'first', 'text': 'First run of your radar: this shows where everything stands today.', 'level': 'info'})
    if al:
        alerts['log'].insert(0, {'at': datetime.now(SYD).isoformat(timespec='minutes'), 'items': al})
        alerts['log'] = alerts['log'][:120]
        deliver(al, today)

    save('prices.json', prices)
    save('announcements.json', anns)
    save('proposals.json', props)
    save('reactions.json', reactions)
    save('alerts.json', alerts)
    save('twin.json', twin)
    save('status.json', {'lastRun': datetime.now(SYD).isoformat(timespec='minutes'), 'errors': ERRORS[:50], 'notes': NOTES[:50],
                         'sources': SOURCES, 'newAnnouncements': len(new_anns), 'newProposals': new_props,
                         'pendingProposals': pending, 'alerts': len(al)})
    log(f'Done. {len(new_anns)} new announcements, {new_props} new review items, {len(al)} alerts, {len(ERRORS)} errors.')
    for e in ERRORS:
        log('  !', e)


if __name__ == '__main__':
    main()
