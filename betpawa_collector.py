#!/usr/bin/env python3
"""
BetPawa VFL Collector — incremental real-time capture.
Polls every 3s during match simulation. Each event is saved the moment its
FT result appears. No waiting for all 66.
"""
import requests, json, time, os, glob
from datetime import datetime, timezone

HEADERS = {
    'x-pawa-brand': 'betpawa-uganda',
    'x-pawa-language': 'en',
    'devicetype': 'web',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}

BASE = 'https://www.betpawa.ug/api/sportsbook/virtual'
SEASONS_URL = f'{BASE}/v1/seasons/list/actual'
EVENTS_URL = f'{BASE}/v2/events/list/by-round/{{round_id}}?page=upcoming'

SAVE_DIR = '/home/voltrix/vfl_data'
os.makedirs(SAVE_DIR, exist_ok=True)

# Postgres writer (optional)
try:
    from db import save_round as pg_save_round
except Exception:
    def pg_save_round(*a, **kw): pass

LEAGUE_IDS = {
    '7794': 'English League', '7795': 'Spanish League', '7796': 'Italian League',
    '9183': 'French League', '9184': 'German League', '13773': 'Portuguese League',
    '13774': 'Dutch League'
}

_seen_rounds = set()

def fetch_json(url):
    for _ in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()
        except:
            time.sleep(1)
    return None

def get_ft_score(r):
    """Return (home_goals, away_goals) only if match has actually finished.
    The API pre-populates FULL_TIME_EXCLUDING_OVERTIME=0 before match starts,
    so we check display.minute >= 90 OR non-zero score to confirm real result."""
    if not r or not r.get('participantPeriodResults'):
        return None, None

    display = r.get('display', {})
    minute = display.get('minute')

    s = {}
    for ppr in r['participantPeriodResults']:
        pt = ppr['participant']['type']
        for pr in ppr['periodResults']:
            if pr['period']['slug'] == 'FULL_TIME_EXCLUDING_OVERTIME':
                s[pt] = int(pr['result'])
    home, away = s.get('HOME'), s.get('AWAY')
    if home is None:
        return None, None

    # Only accept as finished if match display shows 90+ or score is non-zero
    if minute and minute.isdigit() and int(minute) >= 90:
        return home, away
    if home > 0 or away > 0:
        return home, away
    # No display data or minute < 90 with 0-0 = placeholder or in-play
    return None, None

def extract_markets(e):
    odds = {'1x2': {}, 'btts': {}, 'dc': {}, 'ou': [], 'htft': {}}
    for m in e.get('markets', []):
        mn = m['marketType']['name']
        rows = m.get('row', [])
        if mn == '1X2 - FT' and rows:
            for p in rows[0]['prices']:
                odds['1x2'][p['name']] = float(p['price'])
        elif mn == 'Both Teams To Score - FT' and rows:
            for p in rows[0]['prices']:
                odds['btts'][p['name'].lower()] = float(p['price'])
        elif mn == 'Double Chance - FT' and rows:
            for p in rows[0]['prices']:
                odds['dc'][p['name'].lower()] = float(p['price'])
        elif mn == 'Total Score Over/Under - FT':
            for row in rows:
                line = {}
                for p in row['prices']:
                    line[p['name'].lower()] = float(p['price'])
                odds['ou'].append(line)
        elif mn == 'HT / FT':
            for row in rows:
                for p in row['prices']:
                    odds['htft'][p['name']] = float(p['price'])
    return odds

def parse_event(e, cached_odds):
    name = e.get('name', '')
    parts = name.split(' - ')
    home = parts[0] if len(parts) == 2 else name
    away = parts[1] if len(parts) == 2 else '?'
    hg, ag = get_ft_score(e.get('results'))
    comp = e.get('competition', {})
    lid = comp.get('id')
    lname = LEAGUE_IDS.get(lid, comp.get('name', 'Unknown'))
    return {
        'event_id': e['id'],
        'home_team': home,
        'away_team': away,
        'league': lname,
        'league_id': lid,
        'hg': hg,
        'ag': ag,
        'result': f"{hg}:{ag}" if hg is not None else None,
        'timestamp': datetime.now().isoformat() if hg is not None else None,
        'markets': cached_odds.get(e['id'], {}),
    }

def collect():
    global _seen_rounds
    print(f"[betpawa] Incremental — results saved as they arrive", flush=True)

    while True:
        try:
            data = fetch_json(SEASONS_URL)
            if not data:
                time.sleep(5); continue

            now = datetime.now(timezone.utc)
            best = None
            for s in data.get('items', []):
                for rnd in s.get('rounds', []):
                    rid = rnd['id']
                    if rid in _seen_rounds:
                        continue
                    start = datetime.fromisoformat(rnd['tradingTime']['start'].replace('Z','+00:00'))
                    end = datetime.fromisoformat(rnd['tradingTime']['end'].replace('Z','+00:00'))
                    if end > now and (end - now).total_seconds() > -300:
                        if best is None or start < best[1]:
                            best = (rid, start, end, rnd, s)

            if not best:
                time.sleep(5); continue

            rid, start, end, rnd, s = best

            # Wait for trading window
            if start > now:
                w = (start - now).total_seconds()
                if w > 3:
                    print(f"[betpawa] Round {rid} at {start.strftime('%H:%M:%S')} — waiting {w:.0f}s", flush=True)
                    time.sleep(w)

            # Capture markets during trading window
            cached_odds = {}
            for _ in range(20):
                ed = fetch_json(EVENTS_URL.format(round_id=rid))
                if ed and ed.get('items'):
                    for e in ed['items']:
                        m = extract_markets(e)
                        if m['1x2']:
                            cached_odds[e['id']] = m
                    if cached_odds:
                        print(f"[betpawa]   Markets: {len(cached_odds)} events", flush=True)
                        break
                time.sleep(3)

            # Poll during simulation — save results incrementally
            seen_results = set()
            poll_end = time.time() + 300  # 5min max
            last_save = 0

            print(f"[betpawa]   Polling for live results...", flush=True)

            while time.time() < poll_end:
                ed = fetch_json(EVENTS_URL.format(round_id=rid))
                if not ed or not ed.get('items'):
                    time.sleep(3); continue

                events = ed['items']
                new_results = []

                for e in events:
                    hg, ag = get_ft_score(e.get('results'))
                    if hg is not None and e['id'] not in seen_results:
                        seen_results.add(e['id'])
                        p = parse_event(e, cached_odds)
                        new_results.append(p)

                # Save incrementally when new results arrive
                if new_results:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    fn = f'{SAVE_DIR}/betpawa_round_{rid}_{ts}.json'

                    # Load existing partial data
                    existing = []
                    for f in sorted(glob.glob(f'{SAVE_DIR}/betpawa_round_{rid}_*.json')):
                        try:
                            with open(f) as fh:
                                existing += json.load(fh).get('events', [])
                            os.remove(f)
                        except:
                            pass

                    # Merge existing + new (dedup by event_id)
                    merged = {e['event_id']: e for e in existing}
                    for p in new_results:
                        merged[p['event_id']] = p

                    # Compute standings from all saved history
                    all_events = list(merged.values())
                    standings = {}
                    for lid in set(e.get('league_id') for e in all_events if e.get('league_id')):
                        lname = LEAGUE_IDS.get(lid, '')
                        table = {}
                        for ev in all_events:
                            if ev.get('league_id') != lid or not ev.get('result'):
                                continue
                            for team, gf, ga in [(ev['home_team'], ev['hg'], ev['ag']), (ev['away_team'], ev['ag'], ev['hg'])]:
                                row = table.setdefault(team, {'played':0,'w':0,'d':0,'l':0,'gf':0,'ga':0,'pts':0})
                                row['played'] += 1; row['gf'] += gf; row['ga'] += ga
                                if gf > ga: row['w']+=1; row['pts']+=3
                                elif gf == ga: row['d']+=1; row['pts']+=1
                                else: row['l']+=1
                        ranked = sorted(table.items(), key=lambda x: (-x[1]['pts'], -(x[1]['gf']-x[1]['ga']), -x[1]['gf']))
                        standings[lname] = [{'pos':i+1,'team':t,**r,'gd':r['gf']-r['ga']} for i,(t,r) in enumerate(ranked)]

                    with open(fn, 'w') as f:
                        json.dump({
                            'round_id': rid,
                            'collected_at': datetime.now().isoformat(),
                            'source': 'betpawa',
                            'season': s.get('name'),
                            'standings': standings,
                            'events': all_events,
                            'results_count': len([e for e in all_events if e['result']]),
                            'total_events': len(all_events),
                            'complete': len([e for e in all_events if e['result']]) == len(all_events),
                        }, f, indent=2)

                    # Write to SQLite (legacy)
                    try:
                        from db_writer import save_betpawa_round
                        save_betpawa_round(rid, season=s.get('name'), events=all_events, standings=standings)
                    except Exception as e:
                        print(f"  [sqldb] DB write skipped: {e}", flush=True)

                    # Write to Postgres (unified)
                    try:
                        # Split events by league for individual save_round calls
                        league_events = {}
                        for ev in all_events:
                            lname = ev.get('league', 'Unknown')
                            league_events.setdefault(lname, []).append(ev)
                        for lname, evts in league_events.items():
                            pg_matches = []
                            for i, ev in enumerate(evts, 1):
                                if ev.get('hg') is None: continue  # skip no-result events
                                od = ev.get('markets', {}).get('1x2', {})
                                pg_matches.append({
                                    'n': i, 'home': ev['home_team'], 'away': ev['away_team'],
                                    'hg': ev['hg'], 'ag': ev['ag'], 'result': ev['result'],
                                    'outcome': 'W' if ev['hg'] > ev['ag'] else ('D' if ev['hg'] == ev['ag'] else 'L'),
                                    'parity': 'E' if (ev['hg']+ev['ag']) % 2 == 0 else 'O',
                                    'odds': {'1x2': od},
                                })
                            if pg_matches:
                                pg_standings = []
                                for lname2, st_list in standings.items():
                                    if lname2 == lname:
                                        pg_standings = [{'pos': s['pos'], 'team': s['team'],
                                            'points': s['pts'], 'played': s['played'],
                                            'w': s['w'], 'd': s['d'], 'l': s['l'],
                                            'gf': s['gf'], 'ga': s['ga'], 'gd': s['gd']} for s in st_list]
                                pg_save_round(str(rid), 'betpawa', lname, pg_matches, pg_standings)
                    except Exception as e:
                        print(f"  [pg] write skipped: {e}", flush=True)

                    for p in new_results:
                        od = p['markets']['1x2']
                        h = f"{od.get('1','-'):.2f}" if od else '-'
                        d = f"{od.get('X','-'):.2f}" if od else '-'
                        a = f"{od.get('2','-'):.2f}" if od else '-'
                        print(f"  ⚡ {p['result']:<5} {p['home_team']:<8} vs {p['away_team']:<8} [{p['league']:<16}] {h}/{d}/{a}", flush=True)

                    total = len(events)
                    have = len(seen_results)
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {have}/{total} results — saved", flush=True)

                # Check if complete
                if len(seen_results) == len(events):
                    print(f"[betpawa] ✅ Round {rid} complete — all {len(events)} results", flush=True)
                    _seen_rounds.add(rid)
                    break

                time.sleep(3)

            # If we exited the loop but not all results came in
            if rid not in _seen_rounds:
                _seen_rounds.add(rid)
                if seen_results:
                    print(f"[betpawa] Round {rid} — {len(seen_results)}/{len(events)} results collected (timeout)", flush=True)
                else:
                    print(f"[betpawa] Round {rid} — skipped (no results)", flush=True)

        except Exception as e:
            print(f"[betpawa] Error: {e}", flush=True)
            import traceback; traceback.print_exc()
            time.sleep(10)


if __name__ == '__main__':
    collect()
