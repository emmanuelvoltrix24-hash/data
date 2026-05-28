#!/usr/bin/env python3
"""
VFL Railway Runner — deploys all 5 collectors on Railway writing to Postgres.
Runs: betkraft, bongobongo, betpawa, bangbet, bet22
Each collector auto-restarts on crash.
"""
import os, sys, json, time, threading
from datetime import datetime

# ── Ensure DB schema exists on Railway Postgres ─────────────────
from db import init_db as init_postgres
try:
    init_postgres()
    print("[railway] Postgres schema ready", flush=True)
except Exception as e:
    print(f"[railway] Postgres init: {e}", flush=True)

# ── Monkey-patch: inject DATABASE_URL for any collector that needs it ──
os.environ.setdefault('DATABASE_URL', os.environ.get('DATABASE_URL', ''))

# ── Helper: run a collector in a thread with auto-restart ───────
def run_forever(name, module_name, collect_func_name='collect'):
    while True:
        try:
            print(f"[railway] Starting {name}...", flush=True)
            mod = __import__(module_name)
            getattr(mod, collect_func_name)()
        except Exception as e:
            print(f"[railway] {name} crashed: {e} — restarting in 10s", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)

# ── Flask health endpoint ────────────────────────────────────────
from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/')
def health():
    return jsonify({
        'status': 'ok',
        'service': 'vfl-railway',
        'timestamp': datetime.now().isoformat(),
        'collectors': list(COLLECTORS.keys()),
    })

@app.route('/db/stats')
def db_stats():
    try:
        from db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT source, COUNT(*) FROM rounds GROUP BY source")
        rows = cur.fetchall()
        conn.close()
        return jsonify({'rounds_per_source': {r['source']: r['count'] for r in rows}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

COLLECTORS = {
    'betkraft':   {'module': 'railway_collector', 'func': 'collect'},
    'bongobongo': {'module': 'bongobongo_collector', 'func': 'collect'},
    'betpawa':    {'module': 'betpawa_collector', 'func': 'collect'},
    'bangbet':    {'module': 'bangbet_collector', 'func': 'collect'},
    'bet22':      {'module': 'bet22_collector', 'func': 'collect'},
}

def main():
    port = int(os.environ.get('PORT', 8080))

    # Start all collectors in background
    for name, info in COLLECTORS.items():
        t = threading.Thread(target=run_forever, args=(name, info['module'], info['func']), daemon=True)
        t.start()
        print(f"[railway] {name} thread started", flush=True)

    # Run Flask in main thread
    print(f"[railway] Starting Flask on :{port}", flush=True)
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
