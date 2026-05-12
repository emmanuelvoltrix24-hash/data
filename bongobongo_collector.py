#!/usr/bin/env python3
"""
BongoBongo VFL Collector — Railway deployment
Polls latestresult.jsp, prints round data to stdout, saves standings to JSON file.
No auth required. No Playwright.
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

SAVE_DIR = os.environ.get("DATA_DIR", "/data")


def form_pct(hw, hd, hl, aw, ad, al):
    played = hw + hd + hl + aw + ad + al
    return round((hw + aw) * 3 + (hd + ad)) / max(played * 3, 1) * 100


def parse(xml_text):
    root = ET.fromstring(xml_text)

    # Standings (strength = team_id)
    standings = {}
    league = root.find('league')
    if league is not None:
        for t in league.findall('team'):
            tid = t.get('strength')
            hw, hd, hl = int(t.get('hw', 0)), int(t.get('hd', 0)), int(t.get('hl', 0))
            aw, ad, al = int(t.get('aw', 0)), int(t.get('ad', 0)), int(t.get('al', 0))
            played = int(t.get('played', hw + hd + hl + aw + ad + al))
            standings[tid] = {
                'team':     TEAMS.get(tid, tid),
                'position': int(t.get('pos', 0)),
                'points':   int(t.get('pts', 0)),
                'played':   played,
                'hw': hw, 'hd': hd, 'hl': hl,
                'aw': aw, 'ad': ad, 'al': al,
                'gf': int(t.get('hf', 0)) + int(t.get('af', 0)),
                'ga': int(t.get('ha', 0)) + int(t.get('aa', 0)),
                'gd': int(t.get('gd', 0)),
                'form_pct': round(form_pct(hw, hd, hl, aw, ad, al), 1),
            }

    # Upcoming matches with odds
    matches_el = root.find('matches')
    upcoming = None
    if matches_el is not None:
        upcoming = {
            'round_id':  matches_el.get('id'),
            'week':      matches_el.get('week'),
            'season':    matches_el.get('season'),
            'timestamp': matches_el.get('dateString'),
            'matches':   []
        }
        for i, m in enumerate(matches_el.findall('match'), 1):
            h = m.find('home'); d = m.find('draw'); a = m.find('away')
            h_id, a_id = h.get('team'), a.get('team')
            upcoming['matches'].append({
                'n':      i,
                'home':   TEAMS.get(h_id, h_id),
                'away':   TEAMS.get(a_id, a_id),
                'odds':   {'H': float(h.get('odds')), 'D': float(d.get('odds')), 'A': float(a.get('odds'))},
                'h_pos':  standings.get(h_id, {}).get('position'),
                'a_pos':  standings.get(a_id, {}).get('position'),
                'h_pts':  standings.get(h_id, {}).get('points'),
                'a_pts':  standings.get(a_id, {}).get('points'),
                'h_form': standings.get(h_id, {}).get('form_pct'),
                'a_form': standings.get(a_id, {}).get('form_pct'),
            })

    # Latest results
    results_el = root.find('results')
    results = None
    if results_el is not None:
        results = {
            'round_id': results_el.get('id'),
            'week':     results_el.get('week'),
            'season':   results_el.get('season'),
            'matches':  []
        }
        for i, res in enumerate(results_el.findall('result'), 1):
            h_id, a_id = res.get('homeTeam'), res.get('awayTeam')
            hg, ag = int(res.get('homeTeamScore')), int(res.get('awayTeamScore'))
            t = hg + ag
            results['matches'].append({
                'n':      i,
                'home':   TEAMS.get(h_id, h_id),
                'away':   TEAMS.get(a_id, a_id),
                'hg': hg, 'ag': ag,
                'result': f"{hg}:{ag}",
                'parity': None if t == 0 else ('E' if t % 2 == 0 else 'O'),
                'outcome': 'W' if hg > ag else ('L' if hg < ag else 'D'),
            })

    return {'upcoming': upcoming, 'results': results, 'standings': standings}


def save_standings(data):
    os.makedirs(SAVE_DIR, exist_ok=True)
    week = data['results']['week']
    path = f"{SAVE_DIR}/standings_w{week}.json"
    with open(path, 'w') as f:
        json.dump({'week': week, 'season': data['results']['season'],
                   'standings': data['standings']}, f, indent=2)
    return path


def display(data):
    res = data['results']
    upd = data['upcoming']
    ts = datetime.now().strftime('%H:%M:%S')

    print(f"\n[{ts}] NEW ROUND — week={res['week']} round={res['round_id']}", flush=True)
    print("  RESULTS:", flush=True)
    for m in res['matches']:
        print(f"  M{m['n']}: {m['home']:<20} vs {m['away']:<20} | {m['result']} ({m['parity'] or '-'}) {m['outcome']}", flush=True)

    print(f"\n  UPCOMING (week={upd['week']} round={upd['round_id']}):", flush=True)
    print(f"  {'#':<4} {'Home':<20} {'Away':<20} {'H':>6} {'D':>6} {'A':>6}  Pos      Pts      Form%", flush=True)
    print(f"  {'-'*80}", flush=True)
    for m in upd['matches']:
        print(f"  {m['n']:<4} {m['home']:<20} {m['away']:<20} "
              f"{m['odds']['H']:>6} {m['odds']['D']:>6} {m['odds']['A']:>6}  "
              f"{str(m['h_pos'])+'v'+str(m['a_pos']):^8} "
              f"{str(m['h_pts'])+'v'+str(m['a_pts']):^8} "
              f"{str(m['h_form'])+'v'+str(m['a_form'])}", flush=True)


def main():
    last_week = None
    print("🔄 BongoBongo VFL Collector (Railway) started", flush=True)

    while True:
        try:
            r = requests.get(URL, timeout=5)
            if r.status_code != 200:
                time.sleep(5)
                continue

            data = parse(r.text)
            week = data['results']['week'] if data['results'] else None

            if week and week != last_week:
                last_week = week
                sf = save_standings(data)
                display(data)
                print(f"\n  [standings → {sf}]", flush=True)

        except Exception as e:
            print(f"Error: {e}", flush=True)

        time.sleep(5)


if __name__ == '__main__':
    main()
