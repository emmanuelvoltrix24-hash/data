#!/usr/bin/env python3
"""
VFL Collector — Railway deployment
Collects round results + odds, saves to Postgres, exposes REST API.
"""
import os, json, time, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import psycopg
from psycopg.rows import dict_row

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ['DATABASE_URL']
PORT         = int(os.environ.get('PORT', 8080))

LIVE_URL = 'https://vl.betkraft.co.uk/live'
ALL_MARKETS = ['1X2','GG','TG15','TG25','DC','TG35','H1X2','DCH','HS','1X2G',
               '1X2OU15','1X2OU25','1X2OU35','1X2OU45','1X2OU55','CS','DR',
               'FTS','HGG','MG','T1G','T1OU15','T2G','T2OU15','TFG','TG','TGOE']

BK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "tz": "UTC",
    "Referer": "https://legacy-ui.betkraft.co.uk/",
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rounds (
                    round_id BIGINT PRIMARY KEY,
                    league TEXT,
                    competition_id INT,
                    collected_at TIMESTAMP,
                    chain_break BOOLEAN,
                    has_odds BOOLEAN,
                    data JSONB
                )
            """)
        conn.commit()
    print("DB initialized")

def save_round(round_data):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rounds (round_id, league, competition_id, collected_at, chain_break, has_odds, data)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (round_id) DO NOTHING
            """, (
                round_data['round_id'],
                round_data.get('league', 'English'),
                round_data.get('competition_id', 1),
                round_data['collected_at'],
                round_data.get('chain_break', False),
                round_data.get('has_odds', False),
                json.dumps(round_data),
            ))
        conn.commit()

def get_seen_ids():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT round_id FROM rounds")
            return {row['round_id'] for row in cur.fetchall()}


# ── Helpers ───────────────────────────────────────────────────────────────────

def par(hg, ag):
    t = hg + ag
    return None if t == 0 else ('E' if t % 2 == 0 else 'O')

def outcome(hg, ag):
    return 'W' if hg > ag else ('L' if hg < ag else 'D')

def to_utc(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

def get_periods(competition_id=1):
    for _ in range(3):
        try:
            r = requests.get(f'https://vl.betkraft.co.uk/periods/{competition_id}',
                             headers=BK_HEADERS, timeout=15)
            return r.json()['data']['periods']
        except: time.sleep(3)
    return []

def get_live(period):
    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
    for _ in range(3):
        try:
            r = requests.post(LIVE_URL, headers=BK_HEADERS, json=payload, timeout=15).json()
            if r.get('status_code') == 200 and r['data']:
                return r['data'].get('live', [])
            return None
        except: time.sleep(3)
    return None

def fetch_odds(round_number_id, competition_id=1):
    """Fetch only 1X2 odds — fast single request."""
    try:
        payload = {'round_number_id': round_number_id, 'competition_id': competition_id,
                   'country_id': None, 'market_id': '1X2'}
        r = requests.post('https://vl.betkraft.co.uk/data',
                          json=payload, headers=BK_HEADERS, timeout=10)
        data = r.json()
        if data.get('status_code') != 200:
            return {}
        matches = {}
        for m in data['data']['matches']:
            eid = m['event_id']
            matches[eid] = {
                'event_id': eid, 'home_team': m['home_team'],
                'away_team': m['away_team'], 'htf': m.get('htf',''),
                'atf': m.get('atf',''),
                'markets': {'1X2': m['markets'][0]['outcomes']} if m.get('markets') else {}
            }
        return matches
    except:
        return {}


# ── Collector loop ────────────────────────────────────────────────────────────

def collect():
    seen = get_seen_ids()
    chain_broken = True
    pending_odds = {}
    print(f"Collector started. {len(seen)} rounds already in DB.")

    # Pre-fetch odds
    try:
        periods = get_periods()
        if periods:
            p0 = periods[0]
            odds = fetch_odds(p0['round_number_id'], p0['competition_id'])
            if odds:
                pending_odds[p0['round_number_id']] = odds
                print(f"Pre-fetched odds for #{p0['round_number_id']}")
    except Exception as e:
        print(f"Pre-fetch error: {e}")

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
                    print(f"⏳ Round #{rid} in {wait:.0f}s")
                    time.sleep(max(0, wait))

                for _ in range(10):
                    live = get_live(period)
                    if live and len(live) == 10:
                        seen.add(rid)
                        live_sorted = sorted(live, key=lambda m: m['event_id'])
                        odds_map = pending_odds.pop(rid, {})

                        matches = []
                        for i, m in enumerate(live_sorted, 1):
                            hg, ag = map(int, m['result'].split(':'))
                            eid = m['event_id']
                            pre_mkts = odds_map.get(eid, {}).get('markets', {}) if odds_map else {}
                            matches.append({
                                'n': i, 'event_id': eid,
                                'home_team': m['home_team'], 'away_team': m['away_team'],
                                'result': m['result'], 'hg': hg, 'ag': ag,
                                'outcome': outcome(hg, ag), 'parity': par(hg, ag),
                                'pre_markets': pre_mkts,
                            })

                        round_data = {
                            'round_id': rid,
                            'league': 'English',
                            'competition_id': 1,
                            'collected_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'chain_break': chain_broken,
                            'has_odds': any(m['pre_markets'] for m in matches),
                            'matches': matches,
                        }
                        chain_broken = False
                        save_round(round_data)

                        slots = {m['n']: m for m in matches}
                        p5  = slots[5]['parity']  if 5  in slots else '?'
                        p6  = slots[6]['parity']  if 6  in slots else '?'
                        p7  = slots[7]['parity']  if 7  in slots else '?'
                        p10 = slots[10]['parity'] if 10 in slots else '?'
                        ts = datetime.now().strftime('%H:%M:%S')
                        print(f"[{ts}] ✅ #{rid} ({p5},{p6},{p7}) M10={p10} {'✓ odds' if round_data['has_odds'] else '✗'}")

                        # Fetch next round odds
                        try:
                            next_periods = get_periods()
                            next_p = next((p for p in next_periods if p['round_number_id'] not in seen), None)
                            if next_p:
                                odds = fetch_odds(next_p['round_number_id'], next_p['competition_id'])
                                if odds:
                                    pending_odds[next_p['round_number_id']] = odds
                        except Exception as e:
                            print(f"Odds error: {e}")
                        break
                    time.sleep(2)
                else:
                    seen.add(rid)
                break

        except Exception as e:
            print(f"Error: {e}")
        time.sleep(5)


# ── API ───────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def health():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN has_odds THEN 1 ELSE 0 END) as with_odds FROM rounds")
            row = cur.fetchone()
    return jsonify({'status': 'ok', 'rounds': row['total'], 'with_odds': row['with_odds']})

@app.route('/rounds')
def get_rounds():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds ORDER BY round_id DESC LIMIT 200")
            rows = cur.fetchall()
    return jsonify([r['data'] for r in rows])

@app.route('/rounds/latest')
def get_latest():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds ORDER BY round_id DESC LIMIT 1")
            row = cur.fetchone()
    return jsonify(row['data'] if row else {})

@app.route('/rounds/<int:round_id>')
def get_round(round_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds WHERE round_id = %s", (round_id,))
            row = cur.fetchone()
    return jsonify(row['data'] if row else {})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    # Run collector in background thread
    t = threading.Thread(target=collect, daemon=True)
    t.start()
    # Run Flask API
    app.run(host='0.0.0.0', port=PORT)
