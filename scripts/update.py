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
from model import evaluate, trial_pos, parse_date  # noqa: E402

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
def fetch_prices(ticker):
    """Daily closes for ~2 years. Returns list of [YYYY-MM-DD, close]."""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.AX?range=2y&interval=1d'
        j = json.loads(http(url))
        res = j['chart']['result'][0]
        ts = res['timestamp']
        cl = res['indicators']['quote'][0]['close']
        out = []
        for t, c in zip(ts, cl):
            if c is None:
                continue
            d = datetime.fromtimestamp(t, SYD).date().isoformat()
            if out and out[-1][0] == d:
                out[-1][1] = round(c, 4)
            else:
                out.append([d, round(c, 4)])
        if out:
            SOURCES.setdefault(ticker, {})['price'] = 'Yahoo Finance'
            return out
    except Exception as e:
        ERRORS.append(f'{ticker}: Yahoo price failed ({e})')
    # fallback: last price from the ASX site's data provider
    try:
        j = json.loads(http(f'https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/header'))
        d = j.get('data', j)
        px = None
        for k in ('priceLast', 'lastPrice', 'price', 'last'):
            if isinstance(d, dict) and d.get(k) not in (None, ''):
                px = float(d[k]); break
        if px:
            SOURCES.setdefault(ticker, {})['price'] = 'ASX (Markit) last price'
            return [[datetime.now(SYD).date().isoformat(), px]]
    except Exception as e:
        ERRORS.append(f'{ticker}: ASX price fallback failed ({e})')
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


def build_alerts(cfg, companies, prices, positions, new_anns, new_props, state, today, first_run):
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
        # levels
        if last:
            ev = evaluate(co, last, cfg, today)
            if ev['signal'] in ('Buy', 'Sell / trim', 'Hold'):
                tag = ' (draft assumptions — review them)' if ev.get('draft') else ''
                if ev['signal'] == 'Buy':
                    fire(f'zone:{t}', 'Buy', f"**{t}** is at A${last:.3f}, at or below your buy price A${ev['buy']:.3f}{tag}.", 'action')
                elif ev['signal'] == 'Sell / trim':
                    fire(f'zone:{t}', 'Sell', f"**{t}** is at A${last:.3f}, at or above your sell/trim price A${ev['sell']:.3f}{tag}.", 'action')
                else:
                    if state.get(f'zone:{t}') in ('Buy', 'Sell') or first_run:
                        fire(f'zone:{t}', 'Hold', f"**{t}** is in the hold zone (buy ≤ A${ev['buy']:.3f}, sell ≥ A${ev['sell']:.3f}).")
            # big move
            if len(px) >= 2:
                mv = (px[-1][1] / px[-2][1] - 1) * 100
                if abs(mv) >= A.get('bigMovePct', 10):
                    fire(f'move:{t}:{px[-1][0]}', 1, f"**{t}** moved {mv:+.1f}% on {px[-1][0]} (A${px[-2][1]:.3f} → A${px[-1][1]:.3f}).", 'action')
        # position ladder and time limit
        pos = positions.get(t) or {}
        if last and pos.get('plan'):
            for i, tr in enumerate(pos['plan']):
                if not tr.get('done') and tr.get('price') and last >= float(tr['price']):
                    amt = f"{tr['shares']:,} shares" if tr.get('shares') else 'your planned amount'
                    fire(f'ladder:{t}:{i}', 1, f"**{t}** is at A${last:.2f} and reached your sell-ladder step '{tr.get('label','')}' (≥ A${float(tr['price']):.2f}) — sell {amt}.", 'action')
        if pos.get('bought'):
            b = parse_date(pos['bought'])
            maxy = cfg['rules'].get(co['section'], cfg['rules']['other']).get('maxYears', 3)
            if b and today >= b + timedelta(days=int(maxy * 365.25) - 30):
                fire(f'time:{t}', 1, f"**{t}**: your {maxy}-year maximum holding period ends {(b + timedelta(days=int(maxy*365.25))).isoformat()}.", 'action')
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
    title = f"Radar: {len(action)} action, {len(alerts)-len(action)} info — {today.isoformat()}"
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


# ---------------------------------------------------------------- main
def main():
    today = datetime.now(SYD).date()
    cfg = load('config.json', {})
    companies = load('companies.json', {'companies': []})['companies']
    prices = load('prices.json', {})
    anns = load('announcements.json', {})
    props = load('proposals.json', {'items': []})
    decisions = load('decisions.json', {})
    reactions = load('reactions.json', {})
    alerts = load('alerts.json', {'state': {}, 'log': []})
    positions = load('positions.json', {})
    first_run = not alerts.get('state')
    seen_props = {p['id'] for p in props['items']}
    new_anns, new_props = [], 0

    for co in companies:
        t = co['ticker']
        log(f'== {t}')
        hist = fetch_prices(t)
        if hist:
            old = {d: v for d, v in prices.get(t, {}).get('history', [])}
            old.update({d: v for d, v in hist})
            merged = sorted(old.items())[-800:]
            prices[t] = {'history': [[d, v] for d, v in merged], 'last': merged[-1][1], 'lastDate': merged[-1][0],
                         'source': SOURCES.get(t, {}).get('price', '')}
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
    al = build_alerts(cfg, companies, prices, positions, new_anns, new_props, alerts['state'], today, first_run)
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
    save('status.json', {'lastRun': datetime.now(SYD).isoformat(timespec='minutes'), 'errors': ERRORS[:50],
                         'sources': SOURCES, 'newAnnouncements': len(new_anns), 'newProposals': new_props,
                         'pendingProposals': pending, 'alerts': len(al)})
    log(f'Done. {len(new_anns)} new announcements, {new_props} new review items, {len(al)} alerts, {len(ERRORS)} errors.')
    for e in ERRORS:
        log('  !', e)


if __name__ == '__main__':
    main()
