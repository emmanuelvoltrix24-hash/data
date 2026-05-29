#!/usr/bin/env python3
"""
BangBet VFL Collector — results only, polls finished matches.
No odds available. Has period scores (HT/FT). Team names are real.
"""
import requests, json, time, os, glob, threading
from datetime import datetime, timezone

HEADERS = {
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'accept': 'application/json, text/plain, */*',
    'content-type': 'application/json',
    'referer': 'https://www.bangbet.com/virtuals/',
    'origin': 'https://www.bangbet.com',
}

BASE = 'https://bet-api.bangbet.com/api/bet'
TOURNAMENT_URL = f'{BASE}/virtualArea/tournamentList?country=ug&producer=6&sportId=sr:sport:1'
MATCHDAY_URL = f'{BASE}/virtual/match/matchDayList'
RESULTS_URL = f'{BASE}/virtual/match/finished/list'
MATCH_LIST_URL = f'{BASE}/virtual/match/list'

SAVE_DIR = '/home/voltrix/vfl_data'
os.makedirs(SAVE_DIR, exist_ok=True)

# Postgres writer (optional)
try:
    from db import save_round as pg_save_round
except Exception:
    def pg_save_round(*a, **kw): pass

_seen_keys = set()
_pending_odds = {}  # (t_id, st) -> odds_by_team  (proactive cache for upcoming rounds)
_odds_cache_lock = threading.Lock()

def odds_poller():
    """Background thread: polls match/list every 2s to catch the brief odds window."""
    seen_tids = set()
    while True:
        try:
            t_data = fetch(TOURNAMENT_URL)
            if not t_data or not t_data.get('data'):
                time.sleep(2); continue
            for t in t_data['data']:
                tid = t.get('tournamentId')
                if not tid: continue
                ml = fetch(MATCH_LIST_URL, {
                    'producer': 6, 'sportId': 'sr:sport:1',
                    'tournamentId': tid, 'country': 'ug',
                })
                if not ml or not ml.get('data', {}).get('data'):
                    continue
                odds_data = ml['data']['data']
                if not odds_data:
                    continue
                # Parse odds into lookup
                odds_by_team = {}
                for om in odds_data:
                    ht_name = om.get('homeTeamName', '')
                    at_name = om.get('awayTeamName', '')
                    match_name = f"{ht_name} vs. {at_name}"
                    ml2 = om.get('marketList', [])
                    if ml2 and ml2[0].get('markets'):
                        outcomes = ml2[0]['markets'][0].get('outcomes', [])
                        if len(outcomes) >= 3:
                            h_od = outcomes[0].get('odds')
                            d_od = outcomes[1].get('odds')
                            a_od = outcomes[2].get('odds')
                            if h_od and d_od and a_od:
                                od = {'1': float(h_od), 'X': float(d_od), '2': float(a_od)}
                                odds_by_team[ht_name] = od
                                odds_by_team[at_name] = od
                                odds_by_team[match_name] = od
                if odds_by_team:
                    # Try to find which scheduleDate this corresponds to
                    md = fetch(MATCHDAY_URL, {'producer': 6, 'tournamentId': tid, 'country': 'ug'})
                    target_st = None
                    if md and md.get('data'):
                        now_ms = int(time.time() * 1000)
                        for rd in md['data']:
                            sched = rd.get('scheduleDate', 0)
                            if abs(sched - now_ms) < 180000:  # within 3 min
                                target_st = sched
                                break
                    if target_st:
                        with _odds_cache_lock:
                            _pending_odds[(tid, target_st)] = odds_by_team
                            seen_tids.add(tid)
                        print(f"  [odds_poller] Cached {len(odds_by_team)} odds entries for {tid} @ {target_st}", flush=True)
        except Exception as e:
            pass
        time.sleep(2)

def fetch_round_odds(t_id):
    """Fetch odds from match/list for upcoming round and parse into lookup by team name."""
    ml = fetch(MATCH_LIST_URL, {
        'producer': 6, 'sportId': 'sr:sport:1',
        'tournamentId': t_id, 'country': 'ug',
    })
    if not ml or not ml.get('data', {}).get('data'):
        return {}
    odds_by_team = {}
    for om in ml['data']['data']:
        ht_name = om.get('homeTeamName', '')
        at_name = om.get('awayTeamName', '')
        match_name = f"{ht_name} vs. {at_name}"
        ml2 = om.get('marketList', [])
        if ml2 and ml2[0].get('markets'):
            outcomes = ml2[0]['markets'][0].get('outcomes', [])
            if len(outcomes) >= 3:
                h_od = outcomes[0].get('odds')
                d_od = outcomes[1].get('odds')
                a_od = outcomes[2].get('odds')
                if h_od and d_od and a_od:
                    od = {'1': float(h_od), 'X': float(d_od), '2': float(a_od)}
                    odds_by_team[ht_name] = od
                    odds_by_team[at_name] = od
                    odds_by_team[match_name] = od
    return odds_by_team

def fetch(url, payload=None):
    for _ in range(3):
        try:
            if payload:
                r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
            else:
                r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()
        except:
            time.sleep(2)
    return None

def compute_standings(tournament_name):
    """Compute standings from all saved bangbet rounds for a tournament."""
    table = {}
    for fn in glob.glob(f'{SAVE_DIR}/bangbet_*.json'):
        try:
            with open(fn) as f:
                d = json.load(f)
        except:
            continue
        if d.get('tournament') != tournament_name:
            continue
        for m in d.get('matches', []):
            score = m.get('score', '')
            if ':' not in score: continue
            hg, ag = map(int, score.split(':'))
            for team, gf, ga in [(m['home'], hg, ag), (m['away'], ag, hg)]:
                row = table.setdefault(team, {'played':0,'w':0,'d':0,'l':0,'gf':0,'ga':0,'pts':0})
                row['played'] += 1
                row['gf'] += gf; row['ga'] += ga
                if gf > ga: row['w']+=1; row['pts']+=3
                elif gf == ga: row['d']+=1; row['pts']+=1
                else: row['l']+=1
    ranked = sorted(table.items(), key=lambda x: (-x[1]['pts'], -(x[1]['gf']-x[1]['ga']), -x[1]['gf']))
    return [{'pos':i+1,'team':t,'gd':r['gf']-r['ga'],**r} for i,(t,r) in enumerate(ranked)]

def print_match(m, i=1):
    ht = m.get('periods', [{}])[0] if m.get('periods') else {}
    ht_s = f"{ht.get('homeScore','?')}:{ht.get('awayScore','?')}" if ht else '-:-'
    score = m.get('score', '?:?')
    hm = '🔵' if ':' in score and int(score.split(':')[0]) > int(score.split(':')[1]) else ('🟡' if score.split(':')[0]==score.split(':')[1] else '🔴')
    print(f"  {i:>2}. {hm} {m['home']:<20} {score:<6} {m['away']:<20} HT={ht_s}", flush=True)

def collect():
    print(f"[bangbet] Starting — polling tournaments...", flush=True)

    # Start background odds poller (2s interval)
    poller = threading.Thread(target=odds_poller, daemon=True)
    poller.start()

    while True:
        try:
            t_data = fetch(TOURNAMENT_URL)
            if not t_data or not t_data.get('data'):
                time.sleep(30); continue

            for t in t_data['data']:
                t_id = t.get('tournamentId', t.get('id'))
                t_name = t.get('tournamentName', t.get('name', '?'))
                if not t_id: continue

                # Get schedule
                md = fetch(MATCHDAY_URL, {'producer': 6, 'tournamentId': t_id, 'country': 'ug'})
                if not md or not md.get('data'): continue

                for rd in md['data']:
                    st = rd.get('scheduleDate')
                    key = f"{t_id}_{st}"
                    if key in _seen_keys: continue
                    status = rd.get('status', 0)

                    # Fetch odds BEFORE the round finishes (status=0 or 1 = upcoming/live)
                    odds_cache = {}
                    if status in (0, 1):
                        ml = fetch(MATCH_LIST_URL, {
                            'producer': 6, 'sportId': 'sr:sport:1',
                            'tournamentId': t_id, 'country': 'ug',
                        })
                        if ml and ml.get('data', {}).get('data'):
                            for om in ml['data']['data']:
                                ht_name = om.get('homeTeamName', '')
                                at_name = om.get('awayTeamName', '')
                                ml2 = om.get('marketList', [])
                                if ml2 and ml2[0].get('markets'):
                                    outcomes = ml2[0]['markets'][0].get('outcomes', [])
                                    if len(outcomes) >= 3:
                                        h_od = outcomes[0].get('odds')
                                        d_od = outcomes[1].get('odds')
                                        a_od = outcomes[2].get('odds')
                                        if h_od and d_od and a_od:
                                            od = {'1': float(h_od), 'X': float(d_od), '2': float(a_od)}
                                            odds_cache[ht_name] = od
                                            odds_cache[at_name] = od
                            if odds_cache:
                                with _odds_cache_lock:
                                    _pending_odds[key] = odds_cache
                                print(f"  [odds] Cached {len(odds_cache)} odds for {t_name} #{rd.get('no')}", flush=True)
                        continue  # Don't fetch results yet, wait for status=2

                    if status != 2: continue  # 2 = finished

                    # Fetch results
                    res = fetch(RESULTS_URL, {
                        'country': 'ug', 'tournamentId': t_id, 'producer': 6,
                        'sportId': 'sr:sport:1', 'betradarId': rd.get('betradarId'),
                        'number': rd.get('number'), 'seasonId': rd.get('seasonId'),
                        'scheduleDate': st,
                    })

                    if not res or not res.get('data'): continue

                    _seen_keys.add(key)
                    matches = []
                    for m in res['data']:
                        matches.append({
                            'home': m['homeTeamName'],
                            'away': m['awayTeamName'],
                            'score': f"{m['homeScore']}:{m['awayScore']}",
                            'hg': m['homeScore'], 'ag': m['awayScore'],
                            'periods': m.get('periodScoreList', []),
                        })

                    ts = datetime.fromtimestamp(st/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                    round_data = {
                        'tournament': t_name,
                        'tournament_id': t_id,
                        'schedule_time': st,
                        'timestamp': ts,
                        'collected_at': datetime.now().isoformat(),
                        'source': 'bangbet',
                        'matches': matches,
                    }

                    # Compute standings
                    standings = compute_standings(t_name)
                    # Add this round's data temporarily for standings display
                    temp_table = {}
                    for m in matches:
                        hg, ag = m['hg'], m['ag']
                        for team, gf, ga in [(m['home'], hg, ag), (m['away'], ag, hg)]:
                            row = temp_table.setdefault(team, {'played':0,'w':0,'d':0,'l':0,'gf':0,'ga':0,'pts':0})
                            row['played'] += 1; row['gf'] += gf; row['ga'] += ga
                            if gf > ga: row['w']+=1; row['pts']+=3
                            elif gf == ga: row['d']+=1; row['pts']+=1
                            else: row['l']+=1
                    temp_ranked = sorted(temp_table.items(), key=lambda x: (-x[1]['pts'], -(x[1]['gf']-x[1]['ga']), -x[1]['gf']))
                    temp_standings = [{'pos':i+1,'team':t,'gd':r['gf']-r['ga'],**r} for i,(t,r) in enumerate(temp_ranked)]

                    # Save
                    fn = f'{SAVE_DIR}/bangbet_{t_name.replace(" ", "_")}_{st}.json'
                    with open(fn, 'w') as f:
                        json.dump(round_data, f, indent=2)

                    # Write to SQLite (legacy)
                    try:
                        from db_writer import save_bangbet_round
                        save_bangbet_round(st, t_name, tournament_id=t_id,
                                           timestamp=ts, matches=matches, standings=standings)
                    except Exception as e:
                        print(f"  [sqldb] DB write skipped: {e}", flush=True)

                    # Use cached odds (fetched from upcoming round)
                    cache_key = f"{t_id}_{st}"
                    with _odds_cache_lock:
                        odds_by_team = _pending_odds.pop(cache_key, {})
                    if odds_by_team:
                        print(f"  → matched cached odds ({len(odds_by_team)} entries)", flush=True)

                    # Write to Postgres (unified) with odds
                    try:
                        pg_matches = []
                        for i, m in enumerate(matches, 1):
                            score = m.get('score', '?:?')
                            parts = score.split(':')
                            hg, ag = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
                            ht = m.get('ht', m.get('periods', [{}])[0] if m.get('periods') else '')
                            if isinstance(ht, dict):
                                ht = f"{ht.get('homeScore','?')}:{ht.get('awayScore','?')}"
                            # Look up odds for this match
                            match_odds = odds_by_team.get(m['home']) or odds_by_team.get(m['away']) or odds_by_team.get(f"{m['home']} vs. {m['away']}", {})
                            pg_matches.append({
                                'n': i, 'home': m['home'], 'away': m['away'],
                                'hg': hg, 'ag': ag, 'result': score,
                                'outcome': m.get('outcome', 'W' if hg > ag else ('D' if hg == ag else 'L')),
                                'parity': m.get('parity', 'E' if (hg+ag) % 2 == 0 else 'O'),
                                'ht': ht,
                                'odds': {'1x2': match_odds} if match_odds else {},
                            })
                        pg_standings = [{'pos': s['pos'], 'team': s['team'], 'points': s.get('pts', s.get('points')),
                            'played': s['played'], 'w': s.get('w', s.get('wins')),
                            'd': s.get('d', s.get('draws')), 'l': s.get('l', s.get('losses')),
                            'gf': s['gf'], 'ga': s['ga'], 'gd': s.get('gd', s['gf']-s['ga'])} for s in standings]
                        pg_save_round(str(st), 'bangbet', t_name, pg_matches, pg_standings)
                    except Exception as e:
                        print(f"  [pg] write skipped: {e}", flush=True)
                    except:
                        pass

                    # Print
                    print(f"\n{'='*67}", flush=True)
                    print(f"  [bangbet] ✅ {t_name} — {len(matches)} matches @ {ts}", flush=True)
                    print(f"{'='*67}", flush=True)
                    for i, m in enumerate(matches, 1):
                        print_match(m, i)

                    if standings:
                        print(f"\n  🏆 {t_name} Standings (all-time):", flush=True)
                        print(f"  {'POS':<4} {'TEAM':<20} {'PTS':<5} {'W':<4}{'D':<4}{'L':<4} {'GF':<4}{'GA':<4}{'GD':<5}", flush=True)
                        print(f"  {'-'*55}", flush=True)
                        for s in standings[:8]:
                            icon = '🥇' if s['pos']==1 else ('🥈' if s['pos']==2 else ('🥉' if s['pos']==3 else '  '))
                            print(f"  {icon} {s['pos']:<3} {s['team']:<20} {s['pts']:<5} {s['w']:<4}{s['d']:<4}{s['l']:<4} {s['gf']:<4}{s['ga']:<4}{s['gd']:<5}", flush=True)

                    print(f"  → saved: {fn}", flush=True)
                    print(flush=True)

            time.sleep(30)

        except Exception as e:
            print(f"[bangbet] Error: {e}", flush=True)
            import traceback; traceback.print_exc()
            time.sleep(30)


if __name__ == '__main__':
    collect()
