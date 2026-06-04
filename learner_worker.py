"""Single-shot learner worker — runs one source cycle, called by subprocess."""
import os, sys, json, time
import psycopg2
from psycopg2.extras import RealDictCursor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from global_learner import build_fvecs, mine_rules, save_rules, init_tables, load_previous_rules, learn_from_audit

try: init_tables()
except: pass

limit_sizes = {'bongobongo':50, 'betkraft':50, 'bangbet':100}
src_cycle = list(limit_sizes.keys())

# Persist round-robin index in DB so it survives restarts
_conn = psycopg2.connect(os.environ.get("DATABASE_URL", ""))
_c = _conn.cursor()
_c.execute("CREATE TABLE IF NOT EXISTS learner_state (key TEXT PRIMARY KEY, val TEXT)")
_c.execute("SELECT val FROM learner_state WHERE key='roundrobin_idx'")
row = _c.fetchone()
src_idx = int(row[0]) if row else 0
_c.execute("INSERT INTO learner_state(key,val) VALUES('roundrobin_idx',%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
           (str((src_idx + 1) % len(src_cycle)),))
_conn.commit()
_c.close()
_conn.close()

src = src_cycle[src_idx % len(src_cycle)]

t0 = time.time()
prev = load_previous_rules()

# Load rounds for this source
try:
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", ""))
    conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SET statement_timeout = '120s'")
    cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (src, limit_sizes[src]))
    rows = cur.fetchall()
    conn.close()
    rounds = [(r['data'], r['source']) for r in reversed(rows)]
    if len(rounds) >= 10:
        # Step 1: Learn from audit BEFORE mining — adjusts EV on existing rules
        try:
            p, d, dl = learn_from_audit(source=src, max_entries=1000)
            if p + d > 0:
                print(f"[learner] {src} audit: {p} promoted, {d} demoted, {dl} deleted", flush=True)
        except Exception as ea:
            print(f"[learner] audit feedback FAILED: {ea}", flush=True)

        # Step 2: Mine new rules (save_rules preserves adjusted EV via snapshot)
        fv, su = build_fvecs(rounds)
        if fv:
            rules = mine_rules(fv, su, min_hits=4, min_precision=0.70, time_budget=180)
            save_rules(rules, {}, len(rounds), source=src)
            nm = sum(1 for r in rules if any(x in r.get('target','') for x in ['gg_scored','tg25_scored','cs_home','dc_home','margin_group','score_band','cs_away','dc_away','tg45_scored']))
            print(f"[learner] {src}: {len(rules)} rules ({nm} new market) in {time.time()-t0:.0f}s", flush=True)
except Exception as e:
    import traceback; traceback.print_exc()
    print(f"[learner] {src} FAILED: {e}", flush=True)

# Global mine — sample from all sources
try:
    all_rounds = []
    for s in src_cycle:
        try:
            c2 = psycopg2.connect(os.environ.get("DATABASE_URL", ""))
            c2.set_session(readonly=True)
            cu2 = c2.cursor(cursor_factory=RealDictCursor)
            cu2.execute("SET statement_timeout = '60s'")
            cu2.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (s, limit_sizes[s]//2))
            for rw in cu2.fetchall():
                all_rounds.append((rw['data'], rw['source']))
            c2.close()
        except:
            pass
    if len(all_rounds) >= 20:
        # Step 1: Learn from audit BEFORE global mining
        try:
            learn_from_audit(source=None, max_entries=2000)
        except Exception as ea:
            print(f"[learner] global audit feedback FAILED: {ea}", flush=True)

        # Step 2: Mine global rules (preserves adjusted EV)
        fvecs, su2 = build_fvecs(all_rounds)
        if fvecs:
            gl = mine_rules(fvecs, su2, min_hits=8, min_precision=0.75)
            save_rules(gl, prev, len(all_rounds), source='all')
            imp = sum(1 for r in gl if r['precision'] < 1.0)
            print(f"[learner] global: {len(gl)} rules ({imp} imperfect) in {time.time()-t0:.0f}s", flush=True)
except Exception as e:
    print(f"[learner] global FAILED: {e}", flush=True)
