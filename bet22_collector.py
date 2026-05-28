#!/usr/bin/env python3
"""
22Bet VFL Collector — full odds + results when available.
Has 1X2, DC, BTTS, OU (5 lines), Handicap, Individual Total.
Real team names. Idle when no events running.
"""
import requests, json, time, os, glob
from datetime import datetime

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'accept': 'application/json, text/plain, */*',
    'referer': 'https://22bet.ug/virtualsports',
    'origin': 'https://22bet.ug',
}

URL = 'https://22bet.ug/service-api/LineFeed/Get1x2_VZip'
PARAMS = {'champs': '88637', 'count': '50', 'lng': 'en_GB', 'tf': '3000000',
          'mode': '4', 'country': '191', 'partner': '151', 'getEmpty': 'true', 'gr': '337'}

SAVE_DIR = '/home/voltrix/vfl_data'
os.makedirs(SAVE_DIR, exist_ok=True)

_seen_rounds = set()

def fetch():
    for _ in range(3):
        try:
            r = requests.get(URL, params=PARAMS, headers=HEADERS, timeout=15)
            data = r.json()
            if data.get('Success') and data.get('Value'):
                return data['Value']
        except:
            time.sleep(2)
    return []

def parse_ou_key(key):
    """Parse OU line like '3.5' or '2.5'"""
    try:
        return float(key)
    except:
        return 0

def parse_match(m):
    home = m.get('O1', '?')
    away = m.get('O2', '?')
    odds = {'1x2': {}, 'dc': {}, 'btts': {}, 'ou': [], 'handicap': [], 'ind_total': {}}

    # Primary 1X2
    if 'PE' in m:
        for e in m['PE']:
            t = e.get('T', '')
            if t == '1': odds['1x2']['1'] = e.get('C', 0)
            elif t == '2': odds['1x2']['2'] = e.get('C', 0)
            elif t == 'X': odds['1x2']['X'] = e.get('C', 0)

    # Additional markets in AE
    if 'AE' in m:
        for ae_group in m['AE']:
            for me in ae_group.get('ME', []):
                g = me.get('G', '')
                line = me.get('Line', 0)
                team = me.get('Team', '')
                
                if '1X2' in str(g) and 'OU' not in str(g) and 'DC' not in str(g) and 'Handicap' not in str(g):
                    for e in me.get('E', []):
                        odds['1x2'][e.get('T', '')] = e.get('C', 0)
                elif 'DC' in str(g) or 'Double' in str(g):
                    for e in me.get('E', []):
                        odds['dc'][e.get('T', '')] = e.get('C', 0)
                elif 'BTTS' in str(g) or 'Both' in str(g):
                    for e in me.get('E', []):
                        odds['btts'][e.get('T', '').lower()] = e.get('C', 0)
                elif 'OU' in str(g) or 'Over/Under' in str(g):
                    ld = {'line': line, 'over': 0, 'under': 0}
                    for e in me.get('E', []):
                        t = e.get('T', '')
                        if t == 'Over': ld['over'] = e.get('C', 0)
                        elif t == 'Under': ld['under'] = e.get('C', 0)
                    odds['ou'].append(ld)
                elif 'Handicap' in str(g) or g == 5:
                    hc = {'line': line, 'home': 0, 'away': 0}
                    for e in me.get('E', []):
                        t = e.get('T', '')
                        if t == '1' or t == 'Home': hc['home'] = e.get('C', 0)
                        elif t == '2' or t == 'Away': hc['away'] = e.get('C', 0)
                    odds['handicap'].append(hc)
                elif 'Individual' in str(g) or 'Total' in str(g):
                    it = {'team': 'home' if team == 1 else 'away', 'line': line, 'over': 0, 'under': 0}
                    for e in me.get('E', []):
                        t = e.get('T', '')
                        if t == 'Over': it['over'] = e.get('C', 0)
                        elif t == 'Under': it['under'] = e.get('C', 0)
                    key = 'home' if team == 1 else 'away'
                    odds['ind_total'].setdefault(key, []).append(it)

    return {
        'match_id': m.get('I', 0),
        'home_team': home,
        'away_team': away,
        'odds': odds,
    }

def collect():
    global _seen_rounds
    print(f"[22bet] Starting — polling for events...", flush=True)

    while True:
        try:
            events = fetch()
            if not events:
                time.sleep(30)
                continue

            # Group by LI (league ID)
            groups = {}
            for e in events:
                lid = e.get('LI', '') or e.get('I', '')
                groups.setdefault(lid, []).append(e)

            for lid, evts in groups.items():
                if lid in _seen_rounds:
                    continue
                _seen_rounds.add(lid)

                league = evts[0].get('LN', 'Unknown')
                matches = [parse_match(m) for m in evts]

                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                fn = f'{SAVE_DIR}/22bet_round_{lid}_{ts}.json'
                with open(fn, 'w') as f:
                    json.dump({
                        'round_id': lid,
                        'league': league,
                        'collected_at': datetime.now().isoformat(),
                        'source': '22bet',
                        'matches': matches
                    }, f, indent=2)

                # Write to unified DB
                try:
                    from db_writer import save_bet22_round
                    save_bet22_round(lid, league=league, matches=matches)
                except Exception as e:
                    print(f"  [db] DB write skipped: {e}", flush=True)

                # Print
                print(f"\n{'='*60}", flush=True)
                print(f"  [22bet] ✅ {league} — {len(matches)} matches", flush=True)
                print(f"{'='*60}", flush=True)
                for i, m in enumerate(matches[:10], 1):
                    od = m['odds']['1x2']
                    h = f"{od.get('1','-'):.2f}" if od.get('1') else '-'
                    d = f"{od.get('X','-'):.2f}" if od.get('X') else '-'
                    a = f"{od.get('2','-'):.2f}" if od.get('2') else '-'
                    btts = m['odds'].get('btts', {})
                    btts_str = f" GG{'✓' if btts.get('yes') else '✗'}" if btts else ''
                    print(f"  {i:>2}. {m['home_team']:<22} vs {m['away_team']:<22} {h}/{d}/{a}{btts_str}", flush=True)
                if len(matches) > 10:
                    print(f"  ... {len(matches)-10} more", flush=True)
                print(f"  → saved: {fn}", flush=True)
                print(flush=True)

            time.sleep(30)

        except Exception as e:
            print(f"[22bet] Error: {e}", flush=True)
            import traceback; traceback.print_exc()
            time.sleep(30)


if __name__ == '__main__':
    collect()
