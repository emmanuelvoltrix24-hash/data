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

LEARNER_SCRIPT = r'''
import os, sys, json, time, psycopg2
from psycopg2.extras import RealDictCursor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from global_learner import build_fvecs, mine_rules, save_rules, init_tables, load_previous_rules
try: init_tables()
except: pass
limit_sizes = {'bongobongo':50, 'betkraft':50, 'bangbet':100}
src_cycle = list(limit_sizes.keys())
src_idx = int(open("/tmp/learner_idx.txt","r").read().strip()) if os.path.exists("/tmp/learner_idx.txt") else 0
t0 = time.time()
prev = load_previous_rules()
src = src_cycle[src_idx % len(src_cycle)]
limit = limit_sizes[src]
try:
    conn = psycopg2.connect(os.environ.get("DATABASE_URL",""))
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SET statement_timeout = '120s'")
    cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %%s" % limit, (src,))
    rows = cur.fetchall()
    conn.close()
    rounds = [(r["data"], r["source"]) for r in reversed(rows)]
    if len(rounds) >= 10:
        fv, su = build_fvecs(rounds)
        if fv:
            rules = mine_rules(fv, su, min_hits=4, min_precision=0.70)
            save_rules(rules, {}, len(rounds), source=src)
            nm = sum(1 for r in rules if any(x in r.get("target","") for x in ["gg_scored","tg25_scored","cs_home","dc_home","margin_group","score_band","cs_away","dc_away","tg45_scored"]))
            print(f"[learner] {src}: {len(rules)} rules ({nm} new market) in {time.time()-t0:.0f}s")
    # Global mine if we have many rounds from different sources
    all_rounds = []
    for s in src_cycle:
        try:
            c2 = psycopg2.connect(os.environ.get("DATABASE_URL",""))
            c2.set_session(readonly=True)
            cu2 = c2.cursor(cursor_factory=RealDictCursor)
            cu2.execute("SET statement_timeout = '60s'")
            cu2.execute("SELECT data, source FROM rounds WHERE source=%%s ORDER BY round_id DESC LIMIT %d" % (limit//2), (s,))
            for rw in cu2.fetchall():
                all_rounds.append((rw["data"], rw["source"]))
            c2.close()
        except: pass
    if len(all_rounds) >= 20:
        fvecs, su2 = build_fvecs(all_rounds)
        gl = mine_rules(fvecs, su2, min_hits=8, min_precision=0.75)
        save_rules(gl, prev, len(all_rounds), source="all")
        imp = sum(1 for r in gl if r["precision"] < 1.0)
        print(f"[learner] global: {len(gl)} rules ({imp} imperfect) in {time.time()-t0:.0f}s")
except Exception as e:
    import traceback; traceback.print_exc()
with open("/tmp/learner_idx.txt","w") as f:
    f.write(str((src_idx + 1) % len(src_cycle)))
'''

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
    import json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.round_id, p.source, p.slot, p.target, p.pred_type, p.pred_val,
               p.precision, p.hits, p.total, p.confidence, p.created_at::text,
               r.data->'matches' AS raw_matches
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY source ORDER BY created_at DESC) AS rn
            FROM predictions
        ) p
        LEFT JOIN rounds r ON p.round_id::text = r.data->>'round_id' AND p.source = r.data->>'source'
        WHERE p.rn <= 50
        ORDER BY p.created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        entry = {
            'round_id': r[0], 'source': r[1], 'slot': r[2], 'target': r[3],
            'pred_type': r[4], 'pred_val': r[5], 'precision': r[6],
            'hits': r[7], 'total': r[8], 'confidence': r[9], 'created_at': r[10],
        }
        # Extract team names
        slot_num = ''.join(filter(str.isdigit, r[2] or ''))
        try:
            matches = json.loads(r[11]) if isinstance(r[11], str) else (r[11] or [])
            idx = int(slot_num) - 1 if slot_num else -1
            if 0 <= idx < len(matches):
                m = matches[idx]
                home = m.get('home_team', m.get('home', '?'))
                away = m.get('away_team', m.get('away', '?'))
                if home != '?' and away != '?':
                    def abbr(n):
                        p = n.split()
                        return (p[0][:3] + ' ' + p[-1][:2]).upper() if len(p) >= 2 else n[:5].upper()
                    entry['teams'] = f"{abbr(home)} vs {abbr(away)}"
        except:
            pass
        result.append(entry)
    return jsonify({'predictions': result})

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

    # Learner — wrapped in restart loop
    def start_learner():
        while True:
            import subprocess
            proc = subprocess.Popen([sys.executable, 'learner_worker.py'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                out, _ = proc.communicate(timeout=420)
                print(out.decode(errors='replace').rstrip(), flush=True)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                print("[learner] TIMEOUT — killed after 420s", flush=True)
            time.sleep(600)
    lt = threading.Thread(target=start_learner, daemon=True)
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

    # Git backup every 6 hours
    def start_backup():
        time.sleep(300)  # initial delay for DB to populate
        while True:
            try:
                import subprocess
                p = subprocess.run([sys.executable, 'backup_to_git.py'], timeout=300, capture_output=True, text=True)
                if p.stdout: print(p.stdout.rstrip(), flush=True)
                if p.returncode != 0 and p.stderr: print(f"[backup] error: {p.stderr[:200]}", flush=True)
            except subprocess.TimeoutExpired:
                print("[backup] TIMEOUT -- killed after 300s", flush=True)
            except Exception as e:
                print(f"[backup] crashed: {e}", flush=True)
            time.sleep(21600)  # 6 hours
    threading.Thread(target=start_backup, daemon=True).start()
    print(f"[backup] backup thread started (every 6h)", flush=True)

    print(f"[railway] Starting Flask on :{port}", flush=True)
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()


# ── CORS ──────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# ── Teams API ─────────────────────────────────────────────────────────
@app.route('/api/teams')
def api_teams():
    import json
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT data->>'source', data->'matches' FROM rounds WHERE data->'matches' IS NOT NULL ORDER BY collected_at DESC LIMIT 6")
    rows = []
    for r in cur.fetchall():
        try:
            matches = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or [])
            rows.append({"source": r[0], "matches": [{"slot": "M"+str(j+1), "home": m.get("home_team", m.get("home", "?")), "away": m.get("away_team", m.get("away", "?"))} for j,m in enumerate(matches[:10])]})
        except:
            pass
    cur.close()
    return jsonify({"rounds": rows})
