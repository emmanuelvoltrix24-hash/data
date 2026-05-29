#!/usr/bin/env python3
"""
BangBet VFL Collector — results only, polls finished matches.
No odds available. Has period scores (HT/FT). Team names are real.
"""
import requests, json, time, os, glob
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
                    if rd.get('status') != 2: continue  # 2 = finished

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

                    # Write to Postgres (unified)
                    try:
                        pg_matches = []
                        for i, m in enumerate(matches, 1):
                            score = m.get('score', '?:?')
                            parts = score.split(':')
                            hg, ag = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
                            ht = m.get('ht', m.get('periods', [{}])[0] if m.get('periods') else '')
                            if isinstance(ht, dict):
                                ht = f"{ht.get('homeScore','?')}:{ht.get('awayScore','?')}"
                            pg_matches.append({
                                'n': i, 'home': m['home'], 'away': m['away'],
                                'hg': hg, 'ag': ag, 'result': score,
                                'outcome': m.get('outcome', 'W' if hg > ag else ('D' if hg == ag else 'L')),
                                'parity': m.get('parity', 'E' if (hg+ag) % 2 == 0 else 'O'),
                                'ht': ht,
                            })
                        pg_standings = [{'pos': s['pos'], 'team': s['team'], 'points': s.get('pts', s.get('points')),
                            'played': s['played'], 'w': s.get('w', s.get('wins')),
                            'd': s.get('d', s.get('draws')), 'l': s.get('l', s.get('losses')),
                            'gf': s['gf'], 'ga': s['ga'], 'gd': s.get('gd', s['gf']-s['ga'])} for s in standings]
                        pg_save_round(str(st), 'bangbet', t_name, pg_matches, pg_standings)
                    except Exception as e:
                        print(f"  [pg] write skipped: {e}", flush=True)

                    # Also try to get odds from match/list
                    try:
                        ml = fetch(MATCH_LIST_URL, {
                            'producer': 6, 'sportId': 'sr:sport:1',
                            'tournamentId': t_id, 'country': 'ug',
                        })
                        if ml and ml.get('data', {}).get('data'):
                            odds_data = ml['data']['data']
                            odds_fn = fn.replace('.json', '_odds.json')
                            with open(odds_fn, 'w') as f:
                                json.dump({'tournament': t_name, 'schedule_time': st,
                                           'matches_with_odds': odds_data, 'source': 'bangbet'}, f, indent=2)
                            print(f"  → odds saved: {odds_fn}", flush=True)
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
