#!/usr/bin/env python3
"""
VFL Railway Runner — deploys collectors + learner + predictor + auditor + dashboard.
"""
import os, sys, json, time, threading
from datetime import datetime

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
            import traceback; traceback.print_exc()
            time.sleep(10)

def learner_loop():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from global_learner import build_fvecs, mine_rules, save_rules, init_tables, load_previous_rules
    init_tables()
    while True:
        try:
            t0 = time.time()
            prev = load_previous_rules()
            all_rounds = []
            for src, limit in [('bongobongo', 300), ('betkraft', 300), ('bangbet', 800)]:
                try:
                    conn = psycopg2.connect(os.environ.get('DATABASE_URL', ''))
                    cur = conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (src, limit))
                    rows = cur.fetchall()
                    conn.close()
                except:
                    continue
                rounds = [(r['data'], r['source']) for r in reversed(rows)]
                if len(rounds) < 15: continue
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

from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder='dashboard')

# ── Dashboard SPA ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('dashboard', 'index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('dashboard', 'favicon.ico') if os.path.exists('dashboard/favicon.ico') else ('', 204)

@app.route('/manifest.json')
def manifest():
    return send_from_directory('dashboard', 'manifest.json')

@app.route('/sw.js')
def sw():
    return send_from_directory('dashboard', 'sw.js')

# ── API Endpoints ───────────────────────────────────────────────────────────
def get_db():
    import psycopg2
    return psycopg2.connect(os.environ.get('DATABASE_URL', ''))

@app.route('/api/db/stats')
def api_db_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT source, COUNT(*) FROM rounds GROUP BY source")
    rows = cur.fetchall()
    conn.close()
    return jsonify({'rounds_per_source': {r[0]: r[1] for r in rows}})

@app.route('/api/db/storage')
def api_db_storage():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT relname AS table_name,
               pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
               pg_relation_size(relid) AS data_bytes,
               pg_total_relation_size(relid) AS total_bytes
        FROM pg_catalog.pg_statio_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
    """)
    tables = [{'table': r[0], 'size': r[1], 'data_bytes': r[2], 'total_bytes': r[3]} for r in cur.fetchall()]
    
    cur.execute("SELECT pg_database_size(current_database())")
    db_bytes = cur.fetchone()[0]
    db_size = f"{db_bytes/1024/1024:.0f} MB" if db_bytes > 0 else "unknown"
    conn.close()
    return jsonify({'tables': tables, 'db_size': db_size, 'db_bytes': db_bytes})

@app.route('/api/predictions')
def api_predictions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT round_id, source, slot, target, pred_type, pred_val, 
               precision, hits, total, confidence, created_at::text
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY source ORDER BY created_at DESC) AS rn
            FROM predictions
        ) sub
        WHERE rn <= 50
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({'predictions': [{
        'round_id': r[0], 'source': r[1], 'slot': r[2], 'target': r[3],
        'pred_type': r[4], 'pred_val': r[5], 'precision': r[6],
        'hits': r[7], 'total': r[8], 'confidence': r[9], 'created_at': r[10],
    } for r in rows]})

@app.route('/api/audit')
def api_audit():
    from audit import audit_summary
    return jsonify(audit_summary())

@app.route('/api/audit/slots')
def api_audit_slots():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT slot, COUNT(*) as checks,
               SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
               ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy
        FROM audit_log
        GROUP BY slot
        ORDER BY slot
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify([{'slot': r[0], 'checks': r[1], 'correct': r[2], 'accuracy': r[3]} for r in rows])

@app.route('/api/audit/log')
def api_audit_log():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT source, round_id, slot, target, actual_val, was_correct
        FROM audit_log
        ORDER BY checked_at DESC
        LIMIT 50
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({'log': [{
        'source': r[0], 'round_id': r[1], 'slot': r[2], 'target': r[3],
        'actual_val': r[4], 'was_correct': r[5],
    } for r in rows]})

@app.route('/api/rules/top')
def api_rules_top():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT target, conditions::text, precision, ev, hits, total, source, lag, status
        FROM global_rules 
        ORDER BY ev DESC 
        LIMIT 100
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({'rules': [{
        'target': r[0], 'conditions': json.loads(r[1]) if r[1] else {},
        'precision': r[2], 'ev': r[3], 'hits': r[4],
        'total': r[5], 'source': r[6], 'lag': r[7], 'status': r[8],
    } for r in rows]})

@app.route('/api/rounds/recent')
def api_rounds_recent():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT source, round_id::text, collected_at::text
        FROM rounds
        ORDER BY collected_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({'rounds': [{
        'source': r[0], 'round_id': r[1], 'time': r[2],
    } for r in rows]})

COLLECTORS = {
    'betkraft':   {'module': 'railway_collector', 'func': 'collect'},
    'bongobongo': {'module': 'bongobongo_collector', 'func': 'collect'},
    'betpawa':    {'module': 'betpawa_collector', 'func': 'collect'},
    'bangbet':    {'module': 'bangbet_collector', 'func': 'collect'},
    'bet22':      {'module': 'bet22_collector', 'func': 'collect'},
}

def main():
    port = int(os.environ.get('PORT', 8080))

    # Start collectors
    for name, info in COLLECTORS.items():
        t = threading.Thread(target=run_forever, args=(name, info['module'], info['func']), daemon=True)
        t.start()
        print(f"[railway] {name} thread started", flush=True)

    # Learner
    lt = threading.Thread(target=learner_loop, daemon=True)
    lt.start()
    print(f"[railway] learner thread started", flush=True)

    # Global predictor
    def start_predictor():
        time.sleep(30)
        try:
            from global_predictor import main as predictor_main
            predictor_main()
        except Exception as e:
            print(f"[railway] predictor crashed: {e}", flush=True)
            import traceback; traceback.print_exc()
    threading.Thread(target=start_predictor, daemon=True).start()
    print(f"[railway] predictor thread started", flush=True)

    # Auditor
    def start_auditor():
        time.sleep(60)
        try:
            from audit import main as audit_main
            audit_main()
        except Exception as e:
            print(f"[railway] auditor crashed: {e}", flush=True)
            import traceback; traceback.print_exc()
    threading.Thread(target=start_auditor, daemon=True).start()
    print(f"[railway] auditor thread started", flush=True)

    print(f"[railway] Starting Flask on :{port}", flush=True)
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()

