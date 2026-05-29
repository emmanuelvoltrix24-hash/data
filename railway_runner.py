#!/usr/bin/env python3
"""
VFL Railway Runner — deploys all 5 collectors + learner + Flask health endpoint.
Runs: betkraft, bongobongo, betpawa, bangbet, bet22
Learner mines rules every 10 min in a background thread.
"""
import os, sys, json, time, threading
from datetime import datetime

# Ensure DB schema exists on Railway Postgres
from db import init_db as init_postgres
try:
    init_postgres()
    print("[railway] Postgres schema ready", flush=True)
except Exception as e:
    print(f"[railway] Postgres init: {e}", flush=True)

os.environ.setdefault('DATABASE_URL', os.environ.get('DATABASE_URL', ''))

def run_forever(name, module_name, collect_func_name='collect'):
    while True:
        try:
            print(f"[railway] Starting {name}...", flush=True)
            mod = __import__(module_name)
            getattr(mod, collect_func_name)()
        except Exception as e:
            print(f"[railway] {name} crashed: {e} -- restarting in 10s", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)

# Learner thread — runs every 10 min
def learner_loop():
    import sys as _sys, psycopg
    from psycopg.rows import dict_row
    _sys.path.insert(0, os.path.dirname(__file__))
    from global_learner import build_fvecs, mine_rules, save_rules, init_tables, load_previous_rules
    init_tables()
    while True:
        try:
            t0 = time.time()
            prev = load_previous_rules()
            all_rounds = []
            for src, limit in [('bongobongo', 300), ('betkraft', 300), ('bangbet', 800)]:
                try:
                    conn = psycopg.connect(os.environ.get('DATABASE_URL', ''), row_factory=dict_row)
                    cur = conn.cursor()
                    cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (src, limit))
                    rows = cur.fetchall()
                    conn.close()
                except:
                    continue
                rounds = [(r['data'], r['source']) for r in reversed(rows)]
                if len(rounds) < 15:
                    continue
                all_rounds.extend(rounds)
                fv, su = build_fvecs(rounds)
                if fv:
                    rules = mine_rules(fv, su, min_hits=4, min_precision=0.70)
                    save_rules(rules, {}, len(rounds), source=src)
            if len(all_rounds) >= 30:
                fvecs, sources_used = build_fvecs(all_rounds)
                rules = mine_rules(fvecs, sources_used, min_hits=10, min_precision=0.75)
                save_rules(rules, prev, len(all_rounds), source='all')
                imperfect = sum(1 for r in rules if r['precision'] < 1.0)
                print(f"[learner] {len(rules)} global rules ({imperfect} imperfect) in {time.time()-t0:.1f}s", flush=True)
        except Exception as e:
            import traceback; traceback.print_exc()
        time.sleep(600)

# Flask health endpoint
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

@app.route('/rules')
def rules_endpoint():
    try:
        from db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT target, conditions, precision, ev, hits, total, source, lag, status FROM global_rules ORDER BY ev DESC LIMIT 50")
        rows = cur.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
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

    # Start periodic learner
    lt = threading.Thread(target=learner_loop, daemon=True)
    lt.start()
    print(f"[railway] learner thread started", flush=True)

    # Start global prediction engine (DB-driven, no auth, all sources)
    def start_predictor():
        time.sleep(30)
        try:
            from global_predictor import main as predictor_main
            print("[railway] global predictor starting...", flush=True)
            predictor_main()
        except Exception as e:
            print(f"[railway] global predictor crashed: {e}", flush=True)
            import traceback; traceback.print_exc()
    pt = threading.Thread(target=start_predictor, daemon=True)
    pt.start()
    print(f"[railway] global predictor thread started", flush=True)

    # Run Flask in main thread
    print(f"[railway] Starting Flask on :{port}", flush=True)
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
