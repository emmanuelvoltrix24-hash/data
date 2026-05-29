#!/usr/bin/env python3
"""One-shot rule mining — run this on Railway to populate global_rules."""
import sys, time, os, psycopg
from psycopg.rows import dict_row
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from global_learner import init_tables, build_fvecs, mine_rules, save_rules, load_previous_rules

DB = os.environ.get('DATABASE_URL', '')

def load_source(src, limit=1000):
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (src, limit))
            rows = cur.fetchall()
    return [(r['data'], r['source']) for r in reversed(rows)]

init_tables()
prev = load_previous_rules()

configs = [
    ('betkraft', 977,  5,  0.80),
    ('betpawa',  862,  5,  0.80),
    ('bangbet',  500,  10, 0.82),
]

all_rounds = []
for src, limit, min_h, min_p in configs:
    t0 = time.time()
    rounds = load_source(src, limit)
    if len(rounds) < 30:
        print(f"[{src}] skip — {len(rounds)} rounds")
        continue
    all_rounds.extend(rounds)
    fv, su = build_fvecs(rounds)
    if not fv:
        print(f"[{src}] no features")
        continue
    print(f"[{src}] {len(rounds)} rounds, {len(fv[0])} keys", flush=True)
    rules = mine_rules(fv, su, min_hits=min_h, min_precision=min_p)
    save_rules(rules, {}, len(rounds), source=src)
    print(f"  → {len(rules)} rules in {time.time()-t0:.1f}s")
    for r in rules[:3]:
        print(f"    {r['precision']:.0%} ({r['hits']}/{r['total']}) lag={r['lag']} | {r['target']} | IF {r['conditions']}")

# Global
if all_rounds:
    t0 = time.time()
    print(f"\n[all] {len(all_rounds)} rounds...", flush=True)
    fvecs, sources_used = build_fvecs(all_rounds)
    rules = mine_rules(fvecs, sources_used, min_hits=15, min_precision=0.82)
    save_rules(rules, prev, len(all_rounds), source='all')
    print(f"  → {len(rules)} global rules in {time.time()-t0:.1f}s")
    for r in rules[:10]:
        print(f"    {r['precision']:.0%} ({r['hits']}/{r['total']}) lag={r['lag']} src={r.get('sources')} | {r['target']} | IF {r['conditions']}")

print("\nDone.")
