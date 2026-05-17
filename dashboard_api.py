#!/usr/bin/env python3
"""Dashboard API — serves PWA and exposes REST endpoints."""
import os, json
from datetime import datetime
from flask import Flask, jsonify, send_from_directory
import psycopg
from psycopg.rows import dict_row
import requests

app = Flask(__name__, static_folder='dashboard')
DB = os.environ.get('DATABASE_URL', '')

# ── Shared state ──────────────────────────────────────────────────────────────
engine_state = {
    'last_round': None,
    'pattern': None,
    'signal': None,
    'last_updated': None,
    'bets_placed': 0,
    'status': 'idle',
}

bot_config = {
    'running':        True,
    'betting':        True,
    'stake':          50,
    'min_confidence': 'HIGH',
}

BK_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
               "Content-Type": "application/json", "tz": "UTC",
               "Referer": "https://legacy-ui.betkraft.co.uk/"}

def get_db():
    return psycopg.connect(DB, row_factory=dict_row)

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route('/api/status')
def status():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source, COUNT(*) as n, MAX(collected_at) as last FROM rounds GROUP BY source")
            sources = {r['source']: {'rounds': r['n'], 'last': str(r['last'])} for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) as n, SUM(CASE WHEN has_odds THEN 1 ELSE 0 END) as with_odds FROM rounds")
            totals = cur.fetchone()
    return jsonify({'sources': sources, 'total_rounds': totals['n'], 'with_odds': totals['with_odds']})

@app.route('/api/balance')
def balance():
    """Fetch betkraft and bandabets balances and return both in a single response."""
    balances = {}

    # Betkraft balance
    try:
        r = requests.get('https://api.betkraft.co.uk/v1/balance', headers=BK_HEADERS, timeout=5)
        data = r.json()
        if data.get('status_code') == 200:
            balances['betkraft'] = {'balance': data['data']['balance'], 'currency': data['data']['currency']}
        else:
            balances['betkraft'] = {'balance': None, 'currency': 'UGX', 'error': 'session required'}
    except:
        balances['betkraft'] = {'balance': None, 'currency': 'UGX', 'error': 'session required'}

    # Bandabets balance
    try:
        apikey_file = os.path.join(os.path.dirname(__file__), 'bandabets_apikey.txt')
        if os.path.exists(apikey_file):
            api_key = open(apikey_file).read().strip()
            r = requests.get('https://wallet.banda.software/balance?lang=en&country_code=ug',
                             headers={**BK_HEADERS, 'api-key': api_key,
                                      'Origin': 'https://ug.bandabets.com',
                                      'Referer': 'https://ug.bandabets.com/'}, timeout=5)
            if r.status_code == 200:
                d = r.json()
                balances['bandabets'] = {'main': d.get('b2', 0), 'bonus': d.get('b1', 0),
                                         'currency': 'UGX'}
            else:
                balances['bandabets'] = {'error': f'unexpected status {r.status_code}'}
    except Exception as e:
        balances['bandabets'] = {'error': str(e)}

    return jsonify(balances)

@app.route('/api/pnl')
def pnl():
    try:
        with open('data/bets.json') as f:
            bets = json.load(f)
    except:
        bets = []
    settled = [b for b in bets if b['status'] != 'pending']
    won     = [b for b in settled if b['status'] == 'won']
    total_staked = sum(b['stake'] for b in settled)
    total_pnl    = sum(b.get('pnl', 0) or 0 for b in settled)
    return jsonify({
        'total_bets': len(bets),
        'pending': len([b for b in bets if b['status'] == 'pending']),
        'won': len(won),
        'lost': len(settled) - len(won),
        'win_rate': round(len(won)/len(settled)*100) if settled else 0,
        'total_staked': total_staked,
        'total_pnl': round(total_pnl, 2),
        'roi': round(total_pnl/total_staked*100, 1) if total_staked else 0,
        'recent': bets[-10:][::-1],
    })

@app.route('/api/rules')
def rules():
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT target, conditions, lag, hits, total, precision, ev, source, sources
                    FROM global_rules WHERE status='active'
                    ORDER BY precision DESC, hits DESC LIMIT 50
                """)
                rows = [dict(r) for r in cur.fetchall()]
            except:
                rows = []
    return jsonify(rows)

@app.route('/api/prediction')
def prediction():
    """Get prediction for next round based on latest results."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds WHERE source='betkraft' ORDER BY round_id DESC LIMIT 1")
            row = cur.fetchone()
    if not row:
        return jsonify({'error': 'no data'})
    rd = row['data']
    matches = rd.get('matches', [])
    slots = {m['n']: m for m in matches}
    def par(m): t=m['hg']+m['ag']; return None if t==0 else ('E' if t%2==0 else 'O')
    p5  = par(slots[5])  if 5  in slots else None
    p6  = par(slots[6])  if 6  in slots else None
    p7  = par(slots[7])  if 7  in slots else None
    p10 = par(slots[10]) if 10 in slots else None
    # Apply hardcoded rules (until learner rules are wired)
    pat = (p5, p6, p7)
    rules_map = {
        ('E','O','O'): {'rule': 'E,O,O → M10 no loss', 'confidence': 'HIGH', 'outcome': ['W','D']},
        ('O','E','E'): {'rule': 'O,E,E → M10 no loss', 'confidence': 'HIGH', 'outcome': ['W','D']},
        ('O','O','O'): {'rule': 'O,O,O → M10 parity flips', 'confidence': 'HIGH', 'flip': True},
        ('E','E','O'): {'rule': 'E,E,O → M10 parity stable', 'confidence': 'MEDIUM', 'stable': True},
        ('O','E','O'): {'rule': 'O,E,O → M10 Even parity', 'confidence': 'MEDIUM', 'parity': 'E'},
    }
    signal = rules_map.get(pat, {'rule': 'No strong rule', 'confidence': 'LOW'})
    return jsonify({
        'round_id': rd.get('round_id'),
        'pattern': {'M5': p5, 'M6': p6, 'M7': p7, 'M10': p10},
        'signal': signal,
    })

@app.route('/api/activity')
def activity():
    """Latest round per source as bot activity feed."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (source) source, round_id, collected_at, has_odds
                FROM rounds ORDER BY source, round_id DESC
            """)
            return jsonify([dict(r) for r in cur.fetchall()])

# ── PWA static files ──────────────────────────────────────────────────────────

@app.route('/api/engine')
def engine():
    return jsonify({**engine_state, 'config': bot_config})

@app.route('/api/control', methods=['POST'])
def control():
    from flask import request
    data = request.json or {}
    if 'running'        in data: bot_config['running']        = bool(data['running'])
    if 'betting'        in data: bot_config['betting']        = bool(data['betting'])
    if 'stake'          in data: bot_config['stake']          = int(data['stake'])
    if 'min_confidence' in data: bot_config['min_confidence'] = data['min_confidence']
    if data.get('emergency_stop'):
        bot_config['running'] = False
        bot_config['betting'] = False
        engine_state['status'] = 'stopped'
    return jsonify({'ok': True, 'config': bot_config})

@app.route('/')
def index():
    return send_from_directory('dashboard', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('dashboard', path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
