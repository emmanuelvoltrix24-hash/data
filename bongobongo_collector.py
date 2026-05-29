#!/usr/bin/env python3
"""
BongoBongo VFL Collector
Polls latestresult.jsp XML endpoint every 2 seconds.
Returns: odds (1X2), results, standings — all in one request.
"""
import requests
import xml.etree.ElementTree as ET
import json
import time
import os
from datetime import datetime

URL = "https://vgp.sociumhubeurope.com/f1x2games/football1x2/latestresult.jsp?"

TEAMS = {
    "1": "Manchester Blue", "2": "Manchester Red", "3": "Spurs", "4": "London Reds",
    "5": "London Blues", "6": "The Reds", "7": "Newcastle", "8": "WestHam",
    "9": "Brighton", "10": "Fulham", "11": "Brentford", "12": "N. Forest",
    "13": "Sunderland", "14": "Bournemouth", "15": "Burnley", "16": "Everton",
    "17": "Villa", "18": "Wolves", "19": "Leeds", "20": "Palace"
}

SAVE_DIR = '/home/voltrix/vfl_data'
os.makedirs(SAVE_DIR, exist_ok=True)

# Postgres writer (optional — fails silently if DATABASE_URL not set)
try:
    from db import save_round as pg_save_round
except Exception:
    def pg_save_round(*a, **kw): pass

seen_matchdays = set()

def parse(xml_text):
    root = ET.fromstring(xml_text)

    # === Standings ===
    standings = []
    league = root.find('league')
    if league is not None:
        for t in league.findall('team'):
            tid = t.get('strength')
            hw, hd, hl = int(t.get('hw',0)), int(t.get('hd',0)), int(t.get('hl',0))
            aw, ad, al = int(t.get('aw',0)), int(t.get('ad',0)), int(t.get('al',0))
            pts = int(t.get('pts', 0))
            played = hw + hd + hl + aw + ad + al
            gf = int(t.get('hf',0)) + int(t.get('af',0))
            ga = int(t.get('ha',0)) + int(t.get('aa',0))
            standings.append({
                'pos': int(t.get('pos', 0)),
                'team': TEAMS.get(tid, tid),
                'team_id': tid,
                'played': played,
                'points': pts,
                'w': hw + aw,
                'd': hd + ad,
                'l': hl + al,
                'gf': gf,
                'ga': ga,
                'gd': gf - ga,
            })

    # === Upcoming matches with odds ===
    matches_el = root.find('matches')
    upcoming = None
    if matches_el is not None:
        matchday_id = matches_el.get('id')
        upcoming = {
            'matchday_id': matchday_id,
            'week': matches_el.get('week'),
            'season': matches_el.get('season'),
            'timestamp': matches_el.get('dateString'),
            'day': matches_el.get('day'),
            'matches': []
        }
        for i, m in enumerate(matches_el.findall('match'), 1):
            h = m.find('home'); d = m.find('draw'); a = m.find('away')
            h_id, a_id = h.get('team'), a.get('team')
            upcoming['matches'].append({
                'n': i,
                'match_id': m.get('bID'),
                'home': TEAMS.get(h_id, h_id),
                'away': TEAMS.get(a_id, a_id),
                'home_id': h_id,
                'away_id': a_id,
                'odds': {
                    'H': float(h.get('odds')),
                    'D': float(d.get('odds')),
                    'A': float(a.get('odds')),
                },
                'h_pos': next((s['pos'] for s in standings if s['team_id'] == h_id), None),
                'a_pos': next((s['pos'] for s in standings if s['team_id'] == a_id), None),
                'result': None,
            })

    # === Latest results (previous matchday) ===
    results_el = root.find('results')
    results = None
    if results_el is not None:
        results = {
            'matchday_id': results_el.get('id'),
            'week': results_el.get('week'),
            'season': results_el.get('season'),
            'matches': []
        }
        for i, res in enumerate(results_el.findall('result'), 1):
            h_id, a_id = res.get('homeTeam'), res.get('awayTeam')
            hg, ag = int(res.get('homeTeamScore')), int(res.get('awayTeamScore'))
            t = hg + ag
            results['matches'].append({
                'n': i,
                'home': TEAMS.get(h_id, h_id),
                'away': TEAMS.get(a_id, a_id),
                'home_id': h_id,
                'away_id': a_id,
                'score': f"{hg}:{ag}",
                'hg': hg, 'ag': ag,
                'outcome': 'W' if hg > ag else ('D' if hg == ag else 'L'),
                'parity': None if t == 0 else ('E' if t % 2 == 0 else 'O'),
            })

    return upcoming, results, standings


def print_upcoming(upcoming):
    if not upcoming:
        return
    print(f"\n{'='*56}", flush=True)
    print(f"  📋 UPCOMING — Matchday #{upcoming['matchday_id']} (Week {upcoming['week']}, Season {upcoming['season']})", flush=True)
    print(f"  {upcoming['day']} @ {upcoming['timestamp']}", flush=True)
    print(f"{'='*56}", flush=True)
    print(f"  {'#':>2} {'HOME':<16} {'ODDS':<8} {'AWAY':<16}  POS", flush=True)
    print(f"  {'-'*50}", flush=True)
    for m in upcoming['matches']:
        od = m['odds']
        hp = f"({m['h_pos']})" if m['h_pos'] else ''
        ap = f"({m['a_pos']})" if m['a_pos'] else ''
        print(f"  {m['n']:>2}. {m['home']:<16} {od['H']:.2f}/{od['D']:.2f}/{od['A']:.2f} {m['away']:<16} {hp:>4}{ap:>4}", flush=True)

def print_results(results):
    if not results:
        return
    print(f"\n{'='*56}", flush=True)
    print(f"  ✅ RESULTS — Matchday #{results['matchday_id']} (Week {results['week']})", flush=True)
    print(f"{'='*56}", flush=True)
    print(f"  {'#':>2} {'HOME':<16} {'SCORE':<7} {'AWAY':<16} OUT PAR", flush=True)
    print(f"  {'-'*50}", flush=True)
    for m in results['matches']:
        hm = '🔵' if m['hg'] > m['ag'] else ('🟡' if m['hg'] == m['ag'] else '🔴')
        print(f"  {m['n']:>2}. {hm} {m['home']:<16} {m['hg']}:{m['ag']:<3}  {m['away']:<16} {m['outcome']}  {m['parity']}", flush=True)

def print_standings(standings):
    if not standings:
        return
    print(f"\n  {'POS':<4} {'TEAM':<18} {'PTS':<5} {'W':<4}{'D':<4}{'L':<4} {'GF':<4}{'GA':<4}{'GD':<5}", flush=True)
    print(f"  {'-'*50}", flush=True)
    for s in standings[:10]:
        icon = '🥇' if s['pos'] == 1 else ('🥈' if s['pos'] == 2 else ('🥉' if s['pos'] == 3 else ('⬇️' if s['pos'] > 16 else '  ')))
        print(f"  {icon} {s['pos']:<3} {s['team']:<18} {s['points']:<5} {s['w']:<4}{s['d']:<4}{s['l']:<4} {s['gf']:<4}{s['ga']:<4}{s['gd']:<5}", flush=True)


def save_to_json(upcoming, results, standings):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    if upcoming:
        mid = upcoming['matchday_id']
        fn = f'{SAVE_DIR}/bongobongo_upcoming_{mid}_{ts}.json'
        with open(fn, 'w') as f:
            json.dump({'type': 'upcoming', 'data': upcoming, 'standings': standings, 'collected_at': datetime.now().isoformat()}, f, indent=2)
    
    if results:
        mid = results['matchday_id']
        fn = f'{SAVE_DIR}/bongobongo_results_{mid}_{ts}.json'
        with open(fn, 'w') as f:
            json.dump({'type': 'results', 'data': results, 'standings': standings, 'collected_at': datetime.now().isoformat()}, f, indent=2)

        # Write to SQLite (legacy)
        try:
            from db_writer import save_bongobongo_round
            week = int(results['week']) if results.get('week') else None
            season = int(results['season']) if results.get('season') else None
            save_bongobongo_round(mid, week=week, season=season, matches=results['matches'], standings=standings)
        except Exception as e:
            print(f"  [sqldb] DB write skipped: {e}", flush=True)

        # Write to Postgres (unified)
        try:
            pg_matches = [{'n': m['n'], 'home': m['home'], 'away': m['away'],
                           'hg': m['hg'], 'ag': m['ag'], 'result': m['score'],
                           'outcome': m['outcome'], 'parity': m['parity']} for m in results['matches']]
            pg_standings = [{'pos': s['pos'], 'team': s['team'], 'points': s['points'],
                             'played': s['played'], 'w': s['w'], 'd': s['d'], 'l': s['l'],
                             'gf': s['gf'], 'ga': s['ga'], 'gd': s['gd']} for s in standings]
            pg_save_round(str(mid), 'bongobongo', 'English', pg_matches, pg_standings)
        except Exception as e:
            print(f"  [pg] write skipped: {e}", flush=True)

        return fn
    return None


def collect():
    global seen_matchdays
    print(f"[bongobongo] Starting — polling {URL}", flush=True)

    while True:
        try:
            r = requests.get(URL, timeout=10)
            xml_text = r.text

            # Quick sanity check
            if '<end isLoaded="yes">' not in xml_text:
                print("[bongobongo] ⚠️ XML not ready, retrying...", flush=True)
                time.sleep(2)
                continue

            upcoming, results, standings = parse(xml_text)

            # Check if new results are available
            if results and results['matchday_id'] not in seen_matchdays:
                seen_matchdays.add(results['matchday_id'])
                saved = save_to_json(upcoming, results, standings)
                print_results(results)
                print_standings(standings)
                if upcoming:
                    print_upcoming(upcoming)
                print(f"\n  → saved: {saved}", flush=True)
                print(flush=True)

            time.sleep(2)

        except Exception as e:
            print(f"[bongobongo] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(5)


if __name__ == '__main__':
    collect()
