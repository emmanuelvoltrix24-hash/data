#!/usr/bin/env python3
"""
VFL Prediction Engine
Combines M5/M6/M7 parity rules from round N with 1X2 odds + form from round N+1.
"""
import requests, json, time, asyncio
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright
from autobet import bet_on_predictions
from auth import ensure_session, load_cookies

PERIODS_URL = 'https://vl.betkraft.co.uk/periods/1'
LIVE_URL    = 'https://vl.betkraft.co.uk/live'
IFRAME_URL  = "https://ug.bandabets.com/iframe?IsDemo=0&providerID=55&gameName=Euro+Virtuals&gameID=550e8400-e29b-41d4-a716-446655440000"

ensure_session()

def get_cookies():
    ensure_session()
    return load_cookies()


# ── Parity rules from analysis ──────────────────────────────────────────────

def apply_parity_rules(p5, p6, p7, p10_prev):
    """
    Given M5/M6/M7 parity of round N, return prediction for M10 in N+1.
    Returns dict with: parity_pred, outcome_constraint, confidence, rule
    """
    pat = (p5, p6, p7)

    if pat == ('O','O','O'):
        # M10 parity ALWAYS flips (4/4), outcome skews W
        flip = {'E' if p10_prev=='O' else 'O'} if p10_prev else None
        return {'parity': flip, 'outcome': None, 'confidence': 'HIGH', 'rule': 'O,O,O → M10 parity flips'}

    if pat == ('E','O','O'):
        # M10 never loses (7/7)
        return {'parity': None, 'outcome': ['W','D'], 'confidence': 'HIGH', 'rule': 'E,O,O → M10 no loss'}

    if pat == ('O','E','E'):
        # M10 never loses (4/4), parity leans Even
        return {'parity': 'E', 'outcome': ['W','D'], 'confidence': 'HIGH', 'rule': 'O,E,E → M10 no loss, Even'}

    if pat == ('O','E','O'):
        # M10 parity Even 86%
        return {'parity': 'E', 'outcome': None, 'confidence': 'MEDIUM', 'rule': 'O,E,O → M10 Even parity'}

    if pat == ('E','E','O'):
        # M10 parity stays same (84%)
        return {'parity': p10_prev, 'outcome': None, 'confidence': 'MEDIUM', 'rule': 'E,E,O → M10 parity stable'}

    if pat == ('E','E','E'):
        return {'parity': None, 'outcome': None, 'confidence': 'LOW', 'rule': 'E,E,E → neutral'}

    return {'parity': None, 'outcome': None, 'confidence': 'LOW', 'rule': f'{p5},{p6},{p7} → no strong rule'}


# ── Standings + Form ─────────────────────────────────────────────────────────

def get_standings():
    r = requests.get('https://vl.betkraft.co.uk/standing/1/0', cookies=get_cookies(), timeout=10)
    if r.status_code == 200:
        return {s['team_name']: s for s in r.json()['data']['standings']}
    return {}

def form_score(f): return f.count('W')*3 + f.count('D')

def form_trend(f):
    """Recent 3 vs older 3 (positive = improving)."""
    return form_score(f[:3]) - form_score(f[3:]) if len(f) >= 6 else 0

def streak(f):
    """Current streak from most recent result."""
    if not f: return 0, ''
    cur, count = f[0], 1
    for c in f[1:]:
        if c == cur: count += 1
        else: break
    return count, cur

def odds_to_prob(odd): return round(1/float(odd)*100, 1)


# ── Display ──────────────────────────────────────────────────────────────────

def predict_and_display(prev_round, next_matches, standings):
    """Apply rules from prev_round to predict next_matches."""
    if not prev_round or not next_matches:
        return

    def par(m):
        t = m['hg'] + m['ag']
        return None if t==0 else ('E' if t%2==0 else 'O')

    slots = {m['n']: m for m in prev_round['matches']}
    p5  = par(slots[5])  if 5  in slots else None
    p6  = par(slots[6])  if 6  in slots else None
    p7  = par(slots[7])  if 7  in slots else None
    p10 = par(slots[10]) if 10 in slots else None

    rule = apply_parity_rules(p5, p6, p7, p10)

    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n{'='*80}")
    print(f"[{ts}] PREDICTIONS FOR ROUND #{next_matches[0].get('round_id','?')}")
    print(f"  Prev pattern (M5,M6,M7) = ({p5},{p6},{p7})  M10_prev={p10}")
    print(f"  Rule: {rule['rule']}  [{rule['confidence']}]")
    print(f"{'='*80}")
    print(f"  {'#':<4} {'Home':<20} {'Away':<20} {'H%':>5} {'D%':>5} {'A%':>5}  {'Odds':^15}  {'Pos':^7} {'Pts':^7} {'Trend':^7}  Signal")
    print(f"  {'-'*100}")

    for i, m in enumerate(next_matches, 1):
        odds = m['markets'][0]['outcomes']
        h_odd, d_odd, a_odd = float(odds[0]['odd_value']), float(odds[1]['odd_value']), float(odds[2]['odd_value'])
        hp, dp, ap = odds_to_prob(h_odd), odds_to_prob(d_odd), odds_to_prob(a_odd)

        ht, at = m['home_team'], m['away_team']
        hs = standings.get(ht, {})
        as_ = standings.get(at, {})

        h_pos   = hs.get('position', '-')
        a_pos   = as_.get('position', '-')
        h_pts   = hs.get('points', 0)
        a_pts   = as_.get('points', 0)
        h_form  = hs.get('team_form', m.get('htf',''))
        a_form  = as_.get('team_form', m.get('atf',''))

        pos_diff  = (a_pos - h_pos) if isinstance(h_pos, int) and isinstance(a_pos, int) else 0
        pts_diff  = h_pts - a_pts
        h_trend   = form_trend(h_form)
        a_trend   = form_trend(a_form)
        h_streak  = streak(h_form)
        a_streak  = streak(a_form)
        h_fs      = form_score(h_form)
        a_fs      = form_score(a_form)

        # Odds favourite
        odds_fav = 'H' if h_odd < a_odd else 'A'
        form_fav = 'H' if h_fs > a_fs else ('A' if a_fs > h_fs else 'D')
        agree    = odds_fav == form_fav

        # Build signal
        signals = []

        if i == 10 and rule['confidence'] in ('HIGH','MEDIUM'):
            if rule['outcome']:
                allowed = rule['outcome']
                # Pick best outcome among allowed that agrees with odds/form
                candidates = []
                if 'W' in allowed: candidates.append(('H', hp, h_odd))
                if 'D' in allowed: candidates.append(('D', dp, d_odd))
                best = max(candidates, key=lambda x: x[1]) if candidates else None
                if best:
                    signals.append(f"★M10:{best[0]}({rule['confidence']})")
            elif rule['parity']:
                signals.append(f"★M10:par={rule['parity']}({rule['confidence']})")

        # Value: form favours team but odds don't
        if form_fav == 'H' and odds_fav == 'A' and h_odd > 2.5:
            signals.append(f"VAL:H({h_odd:.2f})")
        elif form_fav == 'A' and odds_fav == 'H' and a_odd > 2.5:
            signals.append(f"VAL:A({a_odd:.2f})")

        # Strong form agreement
        if agree and h_streak[0] >= 3 and h_streak[1] == 'W':
            signals.append(f"STK:H{h_streak[0]}W")
        elif agree and a_streak[0] >= 3 and a_streak[1] == 'W':
            signals.append(f"STK:A{a_streak[0]}W")

        # Position gap
        if pos_diff >= 8:
            signals.append(f"POS:H+{pos_diff}")
        elif pos_diff <= -8:
            signals.append(f"POS:A+{abs(pos_diff)}")

        pos_str = f"{h_pos}v{a_pos}" if isinstance(h_pos,int) else '-'
        pts_str = f"{h_pts}v{a_pts}"
        trend_str = f"{h_trend:+d}/{a_trend:+d}"

        print(f"  {i:<4} {ht:<20} {at:<20} {hp:>5} {dp:>5} {ap:>5}  {h_odd:.2f}/{d_odd:.2f}/{a_odd:.2f}  {pos_str:^7} {pts_str:^7} {trend_str:^7}  {' '.join(signals)}")

    print()


# ── Odds fetcher (Playwright) ────────────────────────────────────────────────

async def fetch_odds_async():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        with open('bandabets_cookies.json') as f:
            await context.add_cookies(json.load(f))
        page = await context.new_page()
        captured = {}
        async def on_resp(res):
            if res.url == 'https://vl.betkraft.co.uk/data':
                try:
                    body = await res.text()
                    captured['matches'] = sorted(
                        json.loads(body)['data']['matches'],
                        key=lambda m: m['event_id']
                    )
                except: pass
        context.on('response', on_resp)
        await page.goto(IFRAME_URL, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(15)
        await browser.close()
        return captured.get('matches', [])


# ── Main loop ────────────────────────────────────────────────────────────────

def get_periods():
    return requests.get(PERIODS_URL, cookies=get_cookies(), timeout=10).json()['data']['periods']

def get_live(period):
    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
    r = requests.post(LIVE_URL, cookies=get_cookies(), json=payload, timeout=10).json()
    if r.get('status_code') == 200 and r['data']:
        return r['data'].get('live', [])
    return None

def to_utc(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

def parse_live(live, period):
    matches = sorted(live, key=lambda m: m['event_id'])
    parsed = []
    for i, m in enumerate(matches, 1):
        hg, ag = map(int, m['result'].split(':'))
        parsed.append({'n': i, 'home_team': m['home_team'], 'away_team': m['away_team'],
                       'hg': hg, 'ag': ag, 'result': m['result']})
    return {'round_id': period['round_number_id'], 'matches': parsed}


def build_m10_bets(prev_round, next_matches, standings):
    """Return bet list for M10 based on HIGH confidence parity rule."""
    if not prev_round or not next_matches:
        return []

    def par(m):
        t = m['hg'] + m['ag']
        return None if t == 0 else ('E' if t % 2 == 0 else 'O')

    slots = {m['n']: m for m in prev_round['matches']}
    p5  = par(slots[5])  if 5  in slots else None
    p6  = par(slots[6])  if 6  in slots else None
    p7  = par(slots[7])  if 7  in slots else None
    p10 = par(slots[10]) if 10 in slots else None
    rule = apply_parity_rules(p5, p6, p7, p10)

    if rule['confidence'] != 'HIGH' or not rule['outcome']:
        return []

    # Find M10 in next_matches
    m10 = next_matches[9] if len(next_matches) >= 10 else None
    if not m10:
        return []

    allowed = rule['outcome']  # ['W', 'D'] or similar
    odds = m10['markets'][0]['outcomes']
    h_odd = float(odds[0]['odd_value'])
    d_odd = float(odds[1]['odd_value'])
    a_odd = float(odds[2]['odd_value'])

    # Pick best value outcome among allowed
    candidates = []
    if 'W' in allowed: candidates.append(('1', h_odd, 'H'))
    if 'D' in allowed: candidates.append(('X', d_odd, 'D'))
    if not candidates:
        return []

    best_oid, best_odd, best_label = max(candidates, key=lambda x: x[1])

    return [{
        'event_id':   m10['event_id'],
        'market_id':  '1X2',
        'outcome_id': best_oid,
        'match_desc': f"M10: {m10['home_team']} vs {m10['away_team']}",
        'signal':     f"★{best_label}({rule['rule']}) @{best_odd}",
    }]


print("🔮 VFL Prediction Engine started\n")
seen = set()
prev_round = None
next_odds = []

print("Fetching standings + pre-round odds...")
standings = get_standings()
next_odds = asyncio.run(fetch_odds_async())
if next_odds:
    print(f"✓ Odds: {len(next_odds)} matches | Standings: {len(standings)} teams\n")

while True:
    try:
        periods = get_periods()
        now = datetime.now(timezone.utc)

        for period in periods:
            rid = period['round_number_id']
            if rid in seen:
                continue

            start = to_utc(period['start_time'])
            wait = (start - now).total_seconds()

            if wait > 0:
                print(f"⏳ Round #{rid} in {wait:.0f}s — showing predictions now...")
                if prev_round and next_odds:
                    predict_and_display(prev_round, next_odds, standings)
                    # Build bets for HIGH confidence M10 signals
                    bets = build_m10_bets(prev_round, next_odds, standings)
                    if bets:
                        asyncio.run(bet_on_predictions(bets))
                time.sleep(max(0, wait))

            for _ in range(10):
                live = get_live(period)
                if live and len(live) == 10:
                    seen.add(rid)
                    prev_round = parse_live(live, period)
                    ts = datetime.now().strftime('%H:%M:%S')
                    print(f"\n[{ts}] ✅ RESULTS #{rid}")
                    for m in prev_round['matches']:
                        t = m['hg']+m['ag']
                        par = None if t==0 else ('E' if t%2==0 else 'O')
                        print(f"  M{m['n']}: {m['home_team']} vs {m['away_team']} {m['result']} ({par or '-'})")
                    print("\nFetching next round odds + standings...")
                    standings = get_standings()
                    next_odds = asyncio.run(fetch_odds_async())
                    break
                time.sleep(2)
            else:
                seen.add(rid)
            break

    except Exception as e:
        print(f"Error: {e}")
    time.sleep(5)
