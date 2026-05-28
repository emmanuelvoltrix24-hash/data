#!/usr/bin/env python3
"""
Local Betkraft VFL Collector — saves to JSON files, no DB needed.
Polls /periods → sleeps until kickoff → POSTs /live → POSTs /data for odds → GET /standing for table.
"""
import requests, json, time, os, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'https://vl.betkraft.co.uk'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0',
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/json',
    'Referer': 'https://legacy-ui.betkraft.co.uk/',
    'Origin': 'https://legacy-ui.betkraft.co.uk',
}

SAVE_DIR = '/home/voltrix/vfl_data'
os.makedirs(SAVE_DIR, exist_ok=True)

ALL_MARKETS = ['1X2','GG','TG15','TG25','DC','TG35','H1X2','DCH','HS','1X2G',
               '1X2OU15','1X2OU25','1X2OU35','1X2OU45','1X2OU55','CS','DR',
               'FTS','HGG','MG','T1G','T1OU15','T2G','T2OU15','TFG','TG','TGOE']

def api_get(url):
    return requests.get(url, headers=HEADERS, timeout=15)

def api_post(url, payload):
    return requests.post(url, json=payload, headers=HEADERS, timeout=15)

def to_utc(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

def par(hg, ag):
    t = hg + ag
    return None if t == 0 else ('E' if t % 2 == 0 else 'O')

def outcome(hg, ag):
    return 'W' if hg > ag else ('L' if hg < ag else 'D')

def fetch_all_odds(round_number_id, competition_id=1):
    """Fetch all 27 market odds via POST /data with ThreadPoolExecutor."""
    matches = {}
    lock = threading.Lock()

    def fetch_market(market):
        try:
            payload = {'round_number_id': round_number_id, 'competition_id': competition_id,
                       'country_id': None, 'market_id': market}
            r = api_post(f'{BASE}/data', payload)
            data = r.json()
            if data.get('status_code') != 200:
                return
            for m in data['data']['matches']:
                eid = m['event_id']
                with lock:
                    if eid not in matches:
                        matches[eid] = {'event_id': eid, 'home_team': m['home_team'],
                                        'away_team': m['away_team'], 'htf': m.get('htf',''),
                                        'atf': m.get('atf',''), 'markets': {}}
                    if m.get('markets'):
                        matches[eid]['markets'][market] = m['markets'][0]['outcomes']
        except Exception as e:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(as_completed([ex.submit(fetch_market, mkt) for mkt in ALL_MARKETS]))
    return matches

def fetch_1x2_odds(round_number_id, competition_id=1):
    """Quick single-request for 1X2 odds only."""
    try:
        payload = {'round_number_id': round_number_id, 'competition_id': competition_id,
                   'country_id': None, 'market_id': '1X2'}
        r = api_post(f'{BASE}/data', payload)
        data = r.json()
        if data.get('status_code') != 200:
            return {}
        odds = {}
        for m in data['data']['matches']:
            eid = m['event_id']
            outcomes = {}
            if m.get('markets'):
                for o in m['markets'][0]['outcomes']:
                    key = o['outcome_id']  # '1', 'X', '2'
                    outcomes[key] = float(o['odd_value'])
            odds[eid] = outcomes
        return odds
    except:
        return {}

def fetch_standings(competition_id=1):
    try:
        r = api_get(f'{BASE}/standing/{competition_id}/0')
        data = r.json()
        if data.get('status_code') == 200:
            return data.get('data', {}).get('standings', [])
    except:
        pass
    return []

def build_matches(live, odds_1x2, odds_all):
    matches = []
    for i, m in enumerate(live, 1):
        hg, ag = map(int, m['result'].split(':'))
        eid = m['event_id']
        # 1X2 odds
        o1x2 = odds_1x2.get(eid, {})
        # All markets
        all_mkts = odds_all.get(eid, {}).get('markets', {}) if odds_all else {}
        match = {
            'n': i,
            'event_id': eid,
            'home_team': m['home_team'],
            'away_team': m['away_team'],
            'result': m['result'],
            'hg': hg,
            'ag': ag,
            'outcome': outcome(hg, ag),
            'parity': par(hg, ag),
            'ht': m.get('half_time_scores', ''),
            'odds_1x2': {'H': o1x2.get('1'), 'D': o1x2.get('X'), 'A': o1x2.get('2')},
            'pre_markets': all_mkts,
        }
        matches.append(match)
    return matches

def save_round(round_id, matches, standings, has_odds):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{SAVE_DIR}/round_{round_id}_{ts}.json'
    data = {
        'round_id': round_id,
        'collected_at': datetime.now().isoformat(),
        'source': 'betkraft',
        'match_count': len(matches),
        'has_odds': has_odds,
        'has_standings': bool(standings),
        'standings': standings,
        'matches': matches,
    }
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

    # Also write to unified DB
    try:
        from db_writer import save_betkraft_round
        save_betkraft_round(round_id, matches=matches, standings=standings, has_odds=has_odds)
    except Exception as e:
        print(f"  [db] DB write skipped: {e}", flush=True)

    return filename

def print_matches(matches):
    print(f"  {'#':>2} {'HOME':<16} {'SCORE':<7} {'AWAY':<16} OUT PAR {'H':>5}/{':'.join(['D','A']):>5}  FORM(h/a)", flush=True)
    print(f"  {'-'*65}", flush=True)
    for m in matches:
        hm = '🔵' if m['hg'] > m['ag'] else ('🟡' if m['hg'] == m['ag'] else '🔴')
        od = m['odds_1x2']
        h_od = f"{od['H']:.2f}" if od.get('H') else '-'
        d_od = f"{od['D']:.2f}" if od.get('D') else '-'
        a_od = f"{od['A']:.2f}" if od.get('A') else '-'
        print(f"  {m['n']:>2}. {hm} {m['home_team']:<16} {m['hg']}:{m['ag']:<3}  {m['away_team']:<16} {m['outcome']}  {m['parity']}  {h_od:>5}/{d_od:>4}/{a_od:>4}", flush=True)

def print_standings(standings):
    if not standings:
        return
    print(f"\n  {'POS':<4} {'TEAM':<18} {'PTS':<5} {'FORM':<8} {'W':<4} {'D':<4} {'L':<4}", flush=True)
    print(f"  {'-'*50}", flush=True)
    for s in standings:
        pos = s.get('position', 0)
        team = s.get('team_name', '?')
        pts = s.get('points', 0)
        form = s.get('team_form', '')[:6]
        # form icons
        form_icons = ''.join('🟢' if f == 'W' else ('🟡' if f == 'D' else '🔴') for f in form)
        icon = '🥇' if pos == 1 else ('🥈' if pos == 2 else ('🥉' if pos == 3 else ('⬇️' if pos > 16 else '  ')))
        print(f"  {icon} {pos:<3} {team:<18} {pts:<5} {form_icons:<8} ", flush=True)

def collect():
    seen = set()
    # Load previously collected round IDs from saved files
    for fn in os.listdir(SAVE_DIR):
        if fn.startswith('round_'):
            try:
                parts = fn.split('_')
                seen.add(int(parts[1]))
            except:
                pass

    print(f"[local] Loaded {len(seen)} already-collected rounds in {SAVE_DIR}", flush=True)
    pending_odds_all = {}
    pending_odds_1x2 = {}

    # Pre-fetch odds for first upcoming round
    try:
        r = api_get(f'{BASE}/periods/1')
        periods = r.json()['data']['periods']
        if periods:
            rid0 = periods[0]['round_number_id']
            print(f"[local] Pre-fetching odds for #{rid0}...", flush=True)
            pending_odds_1x2[rid0] = fetch_1x2_odds(rid0)
            pending_odds_all[rid0] = fetch_all_odds(rid0)
            print(f"[local] Odds cached for #{rid0}", flush=True)
    except Exception as e:
        print(f"[local] Pre-fetch error: {e}", flush=True)

    while True:
        try:
            r = api_get(f'{BASE}/periods/1')
            periods = r.json()['data']['periods']
            now = datetime.now(timezone.utc)

            for period in periods:
                rid = period['round_number_id']
                if rid in seen:
                    continue

                start = to_utc(period['start_time'])
                wait = (start - now).total_seconds()
                if wait > 90:
                    continue
                if wait > 0:
                    print(f"[local] Round #{rid} starts in {wait:.0f}s — waiting...", flush=True)
                    time.sleep(max(0, wait))

                # Poll for live results
                for attempt in range(15):
                    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
                    r = api_post(f'{BASE}/live', payload)
                    data = r.json()
                    if data.get('status_code') == 200 and data.get('data',{}).get('live'):
                        live = data['data']['live']
                        if len(live) == 10:
                            seen.add(rid)

                            odds_1x2 = pending_odds_1x2.pop(rid, {})
                            odds_all = pending_odds_all.pop(rid, {})
                            if not odds_1x2:
                                odds_1x2 = fetch_1x2_odds(rid)
                            if not odds_all:
                                odds_all = fetch_all_odds(rid)

                            standings = fetch_standings()
                            matches = build_matches(live, odds_1x2, odds_all)
                            has_odds = bool(odds_1x2) or bool(odds_all)
                            filename = save_round(rid, matches, standings, has_odds)

                            # === DISPLAY ===
                            ts = datetime.now().strftime('%H:%M:%S')
                            print(f"\n{'='*67}", flush=True)
                            print(f"  [{ts}] ✅ ROUND #{rid} — {len(matches)} matches | source: betkraft", flush=True)
                            print(f"{'='*67}", flush=True)
                            print_matches(matches)
                            print(f"{'='*67}", flush=True)
                            if standings:
                                print_standings(standings)
                                print(f"{'='*67}", flush=True)
                            has_o = '✓' if has_odds else '✗'
                            print(f"  → saved: {filename} | odds={has_o} | standings={'✓' if standings else '✗'}", flush=True)
                            print(flush=True)

                            # Pre-fetch odds for next round
                            try:
                                next_r = api_get(f'{BASE}/periods/1')
                                next_periods = next_r.json()['data']['periods']
                                next_p = next((p for p in next_periods if p['round_number_id'] not in seen), None)
                                if next_p:
                                    nrid = next_p['round_number_id']
                                    print(f"[local] Pre-fetching odds for #{nrid}...", flush=True)
                                    pending_odds_1x2[nrid] = fetch_1x2_odds(nrid)
                                    pending_odds_all[nrid] = fetch_all_odds(nrid)
                                    print(f"[local] Odds cached for #{nrid}", flush=True)
                            except Exception as e:
                                print(f"[local] Pre-fetch next error: {e}", flush=True)
                            break
                    time.sleep(2)
                else:
                    print(f"[local] #{rid} — no live data after 15 attempts, marking seen", flush=True)
                    seen.add(rid)
                break  # process one round per loop iteration

        except Exception as e:
            print(f"[local] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(3)

if __name__ == '__main__':
    print(f"[local] Betkraft collector — saving to {SAVE_DIR}/", flush=True)
    collect()
