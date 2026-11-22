"""Offline test: fakes the internet and runs the updater on a copy of the data folder.
Run:  python scripts/test_update.py
"""
import json, os, shutil, sys, tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import update  # noqa: E402

SAMPLE_4C = """Appendix 4C Quarterly cash flow report for entities subject to Listing Rule 4.7B
Name of entity Dimerix Limited ABN Quarter ended ("current quarter") 30 September 2026
Consolidated statement of cash flows Current quarter $A'000 Year to date (3 months) $A'000
1. Cash flows from operating activities
1.1 Receipts from customers 120 120
1.2 Payments for (a) research and development (6,512) (6,512)
(b) product manufacturing and operating costs - -
(e) staff costs (1,204) (1,204)
(f) administration and corporate costs (803) (803)
1.9 Net cash from / (used in) operating activities (7,950) (7,950)
4.6 Cash and cash equivalents at end of period 41,236 41,236
8.6 As a result, estimated quarters of funding available (item 8.5 divided by item 8.3) 5.2
"""


def fake_http(url, binary=False, **kw):
    if 'yahoo' in url:
        base = datetime(2026, 10, 1, 10, 0)
        ts, cl = [], []
        p = 1.0
        for i in range(40):
            d = base + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            ts.append(int(d.timestamp()))
            p = p * (1.25 if i == 20 else 1.0)
            cl.append(round(p, 3))
        return json.dumps({'chart': {'result': [{'timestamp': ts, 'indicators': {'quote': [{'close': cl}]}}]}})
    if 'announcements' in url:
        tk = url.split('/companies/')[1].split('/')[0]
        items = [
            {'documentKey': f'{tk}-1', 'date': '2026-10-21T22:15:00.000Z', 'headline': 'Quarterly Activities/Appendix 4C Cash Flow Report', 'isPriceSensitive': True},
            {'documentKey': f'{tk}-2', 'date': '2026-11-18T21:30:00.000Z', 'headline': 'Trading Halt', 'isPriceSensitive': True},
            {'documentKey': f'{tk}-3', 'date': '2026-11-19T22:00:00.000Z', 'headline': 'AMPLIFY Phase III topline results met primary endpoints', 'isPriceSensitive': True},
            {'documentKey': f'{tk}-4', 'date': '2026-11-10T03:00:00.000Z', 'headline': 'Application for quotation of securities - XYZ', 'isPriceSensitive': False},
            {'documentKey': f'{tk}-5', 'date': '2026-10-02T03:00:00.000Z', 'headline': 'Placement to raise $20 million', 'isPriceSensitive': True},
        ]
        return json.dumps({'data': {'items': items}})
    if '/file/' in url:
        return b'%PDF-fake'
    if 'api.github.com' in url or 'ntfy' in url:
        fake_http.sent.append(url)
        return '{}'
    raise RuntimeError('unexpected url ' + url)


fake_http.sent = []


def main():
    # 4C parser
    p = update.parse_4c(SAMPLE_4C)
    assert p['period'] == '2026-09-30', p
    assert abs(p['cash'] - 41.236) < 1e-6 and abs(p['opcf'] + 7.95) < 1e-6, p
    assert abs(p['rnd'] + 6.512) < 1e-6 and abs(p['staff'] + 1.204) < 1e-6 and abs(p['admin'] + 0.803) < 1e-6, p
    assert p['quartersFunded'] == 5.2, p
    print('4C parser ok:', p)

    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, '..', 'data'), os.path.join(tmp, 'data'))
    for f in ['prices.json', 'announcements.json', 'proposals.json', 'reactions.json', 'alerts.json', 'status.json', 'decisions.json', 'positions.json']:
        fp = os.path.join(tmp, 'data', f)
        if os.path.exists(fp):
            os.remove(fp)
    json.dump({'NEU': {'shares': 38000, 'cost': 15.4, 'bought': '2024-01-10', 'plan': [{'label': 'T1', 'price': 1.1, 'shares': 9500}]}},
              open(os.path.join(tmp, 'data', 'positions.json'), 'w'))
    update.DATA = os.path.join(tmp, 'data')
    update.http = fake_http
    update.read_pdf_text = lambda url: SAMPLE_4C
    update.time.sleep = lambda s: None
    os.environ['GITHUB_TOKEN'] = 'x'; os.environ['GITHUB_REPOSITORY'] = 'me/biotech-radar'; os.environ['GITHUB_REPOSITORY_OWNER'] = 'me'; os.environ['NTFY_TOPIC'] = 'abc'
    update.main()
    D = lambda n: json.load(open(os.path.join(tmp, 'data', n)))
    st = D('status.json'); pr = D('proposals.json'); al = D('alerts.json'); rx = D('reactions.json')
    print('status:', {k: st[k] for k in ['newAnnouncements', 'newProposals', 'pendingProposals', 'alerts']}, 'errors:', st['errors'])
    kinds = sorted({i['kind'] for i in pr['items']})
    print('proposal kinds:', kinds)
    assert 'finance' in kinds and 'halt' in kinds and 'raise' in kinds
    fin = [i for i in pr['items'] if i['kind'] == 'finance'][0]
    assert fin['patches'][0]['op'] == 'addFinance'
    cu = [i for i in pr['items'] if i['ticker'] == 'CU6' and 'AMPLIFY' in i['headline']][0]
    ops = [(x['op'], x.get('status') or x.get('catalystId')) for x in cu['patches']]
    print('CU6 AMPLIFY patches:', ops)
    assert ('trialStatus', 'positive') in ops
    assert any(o == 'catalystDone' for o, _ in ops)
    assert not any('quotation' in i['headline'].lower() for i in pr['items'])
    txt = '\n'.join(a['text'] for a in al['log'][0]['items'])
    print(txt[:1500])
    assert 'TRADING HALT' in txt and 'ladder' in txt
    assert any('github' in u for u in fake_http.sent) and any('ntfy' in u for u in fake_http.sent)
    print('reactions sample:', list(rx['NEU'].values())[:2])
    # second run: nothing new should be re-alerted
    fake_http.sent.clear()
    update.ERRORS.clear()
    update.main()
    st2 = D('status.json')
    assert st2['newProposals'] == 0, st2
    print('second run alerts:', st2['alerts'])
    print('ALL TESTS PASSED')


if __name__ == '__main__':
    main()


def test_model():
    import model
    cfg = json.load(open(os.path.join(HERE, '..', 'data', 'config.json')))
    co = {'ticker': 'TST', 'sleeve': 'monkey', 'animal': 'monkey', 'reviewed': True, 'horizon': '2029-11-20',
          'scenarios': [{'name': 'B', 'prob': 50, 'value': 10, 'success': False}, {'name': 'G', 'prob': 50, 'value': 20, 'success': True}],
          'fundamentals': {'shares': 100, 'eps': 0.5, 'growth': 0.12, 'ebit': 80, 'netDebt': 100, 'fcf': 40, 'investedCapital': 400}}
    hist = [['2026-11-%02d' % d, 5.0, 100000] for d in range(1, 21)]
    m = model.metrics(co, 5.0, hist)
    assert abs(m['pe'] - 10) < 1e-9 and abs(m['peg'] - 10 / 12) < 1e-9, m
    assert abs(m['ev'] - 600) < 1e-9 and abs(m['evEbit'] - 7.5) < 1e-9 and abs(m['fcfYield'] - 0.08) < 1e-9, m
    assert abs(m['roc'] - 0.2) < 1e-9 and abs(m['liquidity'] - 500000) < 1e-6, m
    e = model.evaluate(co, 5.0, cfg, model.date(2026, 11, 22), hist)
    assert e['signal'] == 'Buy', (e['signal'], e['gates'])
    co['fundamentals']['growth'] = 0.30  # outside the Monkey 8-15% band
    e = model.evaluate(co, 5.0, cfg, model.date(2026, 11, 22), hist)
    assert e['signal'] == 'Cheap — fails checks', e['signal']
    co['thesisBroken'] = True
    e = model.evaluate(co, 5.0, cfg, model.date(2026, 11, 22), hist)
    assert e['signal'] == 'Sell — thesis broken'
    assert model.suggest_limit('thesis', 1.234, None, cfg)[0] == 1.205  # 1.209 rounded down to 0.005 tick
    assert model.suggest_limit('over', 2.503, 2.5, cfg)[0] == 2.51
    assert model.suggest_limit('over', 0.0512, 0.05, cfg)[0] == 0.052
    print('model tests ok')


if __name__ == '__main__':
    test_model()
