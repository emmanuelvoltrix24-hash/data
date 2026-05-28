#!/usr/bin/env python3
"""
VFL Collector — Railway deployment (NO AUTH)
Collects round results + odds from betkraft public API, saves to Postgres.
Zero auth, zero cookies, zero Playwright. All endpoints are publicly accessible.
"""
import os, json, time, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from db import init_db, save_round as db_save_round

DATABASE_URL = os.environ['DATABASE_URL']
PORT         = int(os.environ.get('PORT', 8080))

BASE         = 'https://vl.betkraft.co.uk'
PERIODS_URL  = f'{BASE}/periods'
LIVE_URL     = f'{BASE}/live'
DATA_URL     = f'{BASE}/data'
STANDING_URL = f'{BASE}/standing'
RESULTS_URL  = f'{BASE}/results'

ALL_MARKETS = ['1X2','GG','TG15','TG25','DC','TG35','H1X2','DCH','HS','1X2G',
               '1X2OU15','1X2OU25','1X2OU35','1X2OU45','1X2OU55','CS','DR',
               'FTS','HGG','MG','T1G','T1OU15','T2G','T2OU15','TFG','TG','TGOE']

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://legacy-ui.betkraft.co.uk/",
    "Origin": "https://legacy-ui.betkraft.co.uk",
}

# ── Helpers ───────────────────────────────────────────────────────────

def api_get(url):
    return requests.get(url, headers=HEADERS, timeout=15)

def api_post(url, payload):
    return requests.post(url, json=payload, headers=HEADERS, timeout=15)

def par(hg, ag):
    t = hg + ag
    return None if t == 0 else ('E' if t % 2 == 0 else 'O')

def outcome(hg, ag):
    return 'W' if hg > ag else ('L' if hg < ag else 'D')

def to_utc(s):
    """Betkraft timestamps are UTC+3 (EAT)."""
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

# ── DB ────────────────────────────────────────────────────────────────

_seen = set()
_conn = None

def _db():
    global _conn
    from psycopg.rows import dict_row
    import psycopg
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return _conn

def load_seen():
    global _seen
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT round_id FROM rounds WHERE source='betkraft'")
                for row in cur.fetchall():
                    try:
                        _seen.add(int(row['round_id']))
                    except:
                        _seen.add(str(row['round_id']))
    except:
        pass
    return _seen

# ── Betkraft API ──────────────────────────────────────────────────────

def get_periods(competition_id=1):
    for _ in range(3):
        try:
            r = api_get(f'{PERIODS_URL}/{competition_id}')
            return r.json()['data']['periods']
        except:
            time.sleep(2)
    return []

def get_live(period):
    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
    for _ in range(3):
        try:
            r = api_post(LIVE_URL, payload)
            data = r.json()
            if data.get('status_code') == 200 and data.get('data',{}).get('live'):
                return data['data']['live']
            return None
        except:
            time.sleep(2)
    return None

def get_standings(competition_id=1):
    for _ in range(3):
        try:
            r = api_get(f'{STANDING_URL}/{competition_id}/0')
            return r.json()['data']['standings']
        except:
            time.sleep(2)
    return []

def get_results(competition_id=1):
    """Fetch history results. Returns list of completed rounds."""
    for _ in range(3):
        try:
            r = api_get(f'{RESULTS_URL}/{competition_id}/0')
            return r.json()['data']['results']
        except:
            time.sleep(2)
    return []

def fetch_odds(round_number_id, competition_id=1):
    """Fetch all market odds for a round using /data endpoint."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    matches = {}
    lock = threading.Lock()

    def fetch_market(market):
        try:
            payload = {'round_number_id': round_number_id, 'competition_id': competition_id,
                       'country_id': None, 'market_id': market}
            r = api_post(DATA_URL, payload)
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
        except:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(as_completed([ex.submit(fetch_market, mkt) for mkt in ALL_MARKETS]))
    return matches

def build_matches(live_data, odds_map):
    """Convert live API data + pre-fetched odds into match dicts."""
    matches = []
    for m in live_data:
        hg, ag = map(int, m['result'].split(':'))
        eid = m['event_id']
        pre_mkts = odds_map.get(eid, {}).get('markets', {}) if odds_map else {}
        matches.append({
            'n': len(matches) + 1,
            'event_id': eid,
            'home_team': m['home_team'],
            'away_team': m['away_team'],
            'result': m['result'],
            'hg': hg, 'ag': ag,
            'outcome': outcome(hg, ag),
            'parity': par(hg, ag),
            'ht': m.get('half_time_scores', ''),
            'home_score_times': m.get('home_score_times', []),
            'away_score_times': m.get('away_score_times', []),
            'pre_markets': pre_mkts,
        })
    return sorted(matches, key=lambda x: x['event_id'])

def save_round(rid, season_id, competition_id, league, matches, standings, has_odds, chain_break):
    """Save a round via unified db.py schema."""
    extra = {
        'season_id': season_id,
        'competition_id': competition_id,
        'chain_break': chain_break,
    }
    db_save_round(str(rid), 'betkraft', league, matches, standings, extra)

def backfill_missed():
    """Check /results for rounds not in DB and save them."""
    global _seen
    results = get_results()
    if not results:
        return
    saved = 0
    for rnd in reversed(results):
        rid = int(rnd['round_id'])
        if rid in _seen:
            continue
        matches = []
        for i, m in enumerate(rnd.get('matches', []), 1):
            hg, ag = map(int, m['result'].split(':'))
            matches.append({
                'n': i,
                'event_id': m['event_id'],
                'home_team': m['home_team'],
                'away_team': m['away_team'],
                'result': m['result'],
                'hg': hg, 'ag': ag,
                'outcome': outcome(hg, ag),
                'parity': par(hg, ag),
                'ht': m.get('half_time_scores', ''),
                'home_score_times': m.get('home_score_times', []),
                'away_score_times': m.get('away_score_times', []),
            })
        standings = get_standings(rnd.get('competition_id', 1))
        save_round(rid, int(rnd.get('season_id', 0)), rnd.get('competition_id', 1),
                   rnd.get('competition_name', 'English'), matches, standings, has_odds=False, chain_break=False)
        _seen.add(rid)
        saved += 1
        print(f"[backfill] Saved #{rid} ({matches[0]['result']}, {matches[1]['result']}, ...)", flush=True)
    if saved:
        print(f"[backfill] Saved {saved} missed rounds", flush=True)

# ── Collector loop ────────────────────────────────────────────────────

def collect():
    global _seen
    seen = load_seen()
    print(f"[collector] Started. {len(seen)} rounds in DB.", flush=True)

    # Backfill missed rounds from /results
    try:
        backfill_missed()
    except Exception as e:
        print(f"[collector] Backfill error: {e}", flush=True)
    seen = load_seen()

    chain_broken = True
    pending_odds = {}

    # Pre-fetch odds for upcoming round
    try:
        periods = get_periods()
        if periods:
            p0 = periods[0]
            odds = fetch_odds(p0['round_number_id'])
            if odds:
                pending_odds[p0['round_number_id']] = odds
                print(f"[collector] Pre-fetched odds for #{p0['round_number_id']}", flush=True)
    except Exception as e:
        print(f"[collector] Pre-fetch error: {e}", flush=True)

    while True:
        try:
            # Periodic backfill check (every 50 rounds)
            if len(seen) % 50 == 0:
                try:
                    backfill_missed()
                except:
                    pass

            periods = get_periods()
            if not periods:
                print("[collector] No periods — retry in 5s", flush=True)
                time.sleep(5)
                continue

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
                    print(f"[collector] Round #{rid} in {wait:.0f}s", flush=True)
                    time.sleep(max(0, wait))

                # Poll for results
                for attempt in range(10):
                    live = get_live(period)
                    if live and len(live) == 10:
                        seen.add(rid)
                        odds_map = pending_odds.pop(rid, {})
                        season_id = live[0].get('season_id')
                        matches = build_matches(live, odds_map)

                        has_odds = any(m['pre_markets'] for m in matches)
                        save_round(rid, int(season_id) if season_id else None, 1,
                                   'English', matches, get_standings(), has_odds, chain_broken)
                        chain_broken = False

                        slots = {m['n']: m for m in matches}
                        p5  = slots[5]['parity']  if 5  in slots else '?'
                        p6  = slots[6]['parity']  if 6  in slots else '?'
                        p7  = slots[7]['parity']  if 7  in slots else '?'
                        p10 = slots[10]['parity'] if 10 in slots else '?'
                        ts = datetime.now().strftime('%H:%M:%S')
                        has_o = '\u2713 odds' if round_data['has_odds'] else '\u2717'
                        print(f"[{ts}] \u2705 #{rid} ({p5},{p6},{p7}) M10={p10} {has_o}", flush=True)

                        # Pre-fetch next round odds
                        try:
                            next_periods = get_periods()
                            next_p = next((p for p in next_periods if p['round_number_id'] not in seen), None)
                            if next_p:
                                odds = fetch_odds(next_p['round_number_id'])
                                if odds:
                                    pending_odds[next_p['round_number_id']] = odds
                                    print(f"[collector] Pre-fetched odds for #{next_p['round_number_id']}", flush=True)
                        except Exception as e:
                            print(f"[collector] Pre-fetch error: {e}", flush=True)
                        break

                    time.sleep(2)
                else:
                    seen.add(rid)
                    print(f"[collector] #{rid} — no live data after 10 attempts, checking /results", flush=True)
                    # Try /results as fallback
                    try:
                        backfill_missed()
                    except:
                        pass
                break  # process one round per loop iteration

        except Exception as e:
            print(f"[collector] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(5)

# ── Flask API ──────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def health():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN has_odds THEN 1 ELSE 0 END) as with_odds
                FROM rounds WHERE source='betkraft'
            """)
            row = cur.fetchone()
    return jsonify({
        'status': 'ok',
        'rounds': row['total'],
        'with_odds': row['with_odds'],
        'source': 'betkraft'
    })

@app.route('/rounds')
def get_rounds():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT data FROM rounds
                WHERE source='betkraft'
                ORDER BY round_id DESC LIMIT 200
            """)
            rows = cur.fetchall()
    return jsonify([r['data'] for r in rows])

@app.route('/rounds/latest')
def get_latest():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT data FROM rounds
                WHERE source='betkraft'
                ORDER BY round_id DESC LIMIT 1
            """)
            row = cur.fetchone()
    return jsonify(row['data'] if row else {})

@app.route('/rounds/<round_id>')
def get_round(round_id):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT data FROM rounds
                WHERE source='betkraft' AND round_id = %s
            """, (str(round_id),))
            row = cur.fetchone()
    return jsonify(row['data'] if row else {})

# ── Main ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("[main] Initializing DB...", flush=True)
    init_db()

    print("[main] Starting collector thread...", flush=True)
    t = threading.Thread(target=collect, daemon=True)
    t.start()

    print(f"[main] Starting Flask on port {PORT}...", flush=True)
    app.run(host='0.0.0.0', port=PORT)
