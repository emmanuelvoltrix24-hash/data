#!/usr/bin/env python3
"""One-shot rule mining — run this on Railway to populate global_rules."""
import sys, time, os, psycopg2
from psycopg2.extras import RealDictCursor
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from global_learner import init_tables, build_fvecs, mine_rules, save_rules, load_previous_rules

DB = os.environ.get('DATABASE_URL', '')

def normalize_data(data):
    """Normalize match keys across sources: 'home'→'home_team', 'away'→'away_team'.
    Also normalizes standings: 'pos'→'position', 'team'→'team_name' for betkraft compat."""
    matches = data.get('matches', [])
    for m in matches:
        if 'home' in m and 'home_team' not in m:
            m['home_team'] = m['home']
        if 'away' in m and 'away_team' not in m:
            m['away_team'] = m['away']
        # Normalize odds from odds.1x2 into pre_markets format if present
        if 'odds' in m and 'pre_markets' not in m:
            x2 = m['odds'].get('1x2', {})
            if x2:
                m['pre_markets'] = {'1X2': [
                    {'odd_value': str(x2.get('1', ''))},
                    {'odd_value': str(x2.get('X', ''))},
                    {'odd_value': str(x2.get('2', ''))},
                ]}
    # Normalize standings
    standings = data.get('standings', [])
    for s in standings:
        if 'pos' in s and 'position' not in s:
            s['position'] = s['pos']
        if 'team_name' not in s and 'team' in s:
            s['team_name'] = s['team']
        if 'team' not in s and 'team_name' in s:
            s['team'] = s['team_name']
    return data

def load_source(src, limit=1000):
    with psycopg2.connect(DB) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s", (src, limit))
            rows = cur.fetchall()
    return [(normalize_data(r['data']), r['source']) for r in reversed(rows)]

init_tables()
prev = load_previous_rules()

configs = [
    ('betkraft',  200, 5,  0.80),
    ('bangbet',   500, 5,  0.80),
    ('bongobongo', 200, 5,  0.80),
    ('betpawa',   200, 3,  0.75),
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

