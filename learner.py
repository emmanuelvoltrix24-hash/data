#!/usr/bin/env python3
"""
VFL Pattern Learner
Reads collected rounds from Postgres, mines all available fields for
high-precision rules, writes discovered rules back to a `rules` table.
Runs every RUN_EVERY seconds.
"""
import os, json, time, itertools
from collections import Counter, defaultdict
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ['DATABASE_URL']
MIN_ROUNDS   = 20
RUN_EVERY    = 300  # 5 minutes


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_rules_table():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    id SERIAL PRIMARY KEY,
                    target TEXT,
                    conditions JSONB,
                    hits INT,
                    total INT,
                    precision FLOAT,
                    recall FLOAT,
                    discovered_at TIMESTAMP,
                    rounds_used INT
                )
            """)
        conn.commit()


def load_rounds():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds ORDER BY round_id ASC")
            return [r['data'] for r in cur.fetchall()]


# ── Feature extraction ────────────────────────────────────────────────────────

def par(hg, ag):
    t = hg + ag
    return None if t == 0 else ('E' if t % 2 == 0 else 'O')

def outcome(hg, ag):
    return 'W' if hg > ag else ('L' if hg < ag else 'D')

def form_score(f):
    return f.count('W') * 3 + f.count('D') if f else 0

def form_trend(f):
    if len(f) < 6: return 0
    return form_score(f[:3]) - form_score(f[3:])

def discretize(val, key):
    if val is None: return None
    if key in ('h_odd', 'd_odd', 'a_odd'):
        if val < 1.5: return 'very_low'
        if val < 2.0: return 'low'
        if val < 2.5: return 'med'
        if val < 3.5: return 'high'
        return 'very_high'
    if key == 'pos_diff':
        if val <= -8: return 'H_much_better'
        if val <= -3: return 'H_better'
        if val <= 3:  return 'even'
        if val <= 8:  return 'A_better'
        return 'A_much_better'
    if key == 'pts_diff':
        if val >= 6:  return 'H_dominant'
        if val >= 2:  return 'H_ahead'
        if val >= -2: return 'even'
        if val >= -6: return 'A_ahead'
        return 'A_dominant'
    if key == 'form_diff':
        if val >= 6:  return 'H_form'
        if val >= 2:  return 'H_slight'
        if val >= -2: return 'even'
        if val >= -6: return 'A_slight'
        return 'A_form'
    return val


def extract_match_features(m, standings_map):
    hg, ag = m['hg'], m['ag']
    hs  = standings_map.get(m['home_team'], {})
    as_ = standings_map.get(m['away_team'], {})
    h_form = hs.get('team_form', '')
    a_form = as_.get('team_form', '')
    h_pos  = hs.get('position', 10)
    a_pos  = as_.get('position', 10)

    mkts   = m.get('pre_markets', {})
    x2     = mkts.get('1X2', [])
    h_odd  = float(x2[0]['odd_value']) if len(x2) > 0 else None
    d_odd  = float(x2[1]['odd_value']) if len(x2) > 1 else None
    a_odd  = float(x2[2]['odd_value']) if len(x2) > 2 else None

    gg     = mkts.get('GG', [])
    gg_odd = next((float(o['odd_value']) for o in gg if o.get('outcome_id') == 'Yes'), None)

    tg25   = mkts.get('TG25', [])
    ov25   = next((float(o['odd_value']) for o in tg25 if 'over' in o.get('outcome_name','').lower()), None)

    tgoe   = mkts.get('TGOE', [])
    ev_odd = next((float(o['odd_value']) for o in tgoe if o.get('outcome_id') == 'Even'), None)

    odds_fav_val = None
    if h_odd and d_odd and a_odd:
        if h_odd < a_odd and h_odd < d_odd: odds_fav_val = 'H'
        elif a_odd < h_odd and a_odd < d_odd: odds_fav_val = 'A'
        else: odds_fav_val = 'D'

    return {
        'n':           m['n'],
        'outcome':     outcome(hg, ag),
        'parity':      par(hg, ag),
        'cs':          hg == 0 or ag == 0,
        'both_score':  hg > 0 and ag > 0,
        'total':       hg + ag,
        'margin':      abs(hg - ag),
        'h_pos':       h_pos,
        'a_pos':       a_pos,
        'pos_diff':    a_pos - h_pos,
        'pts_diff':    hs.get('points', 0) - as_.get('points', 0),
        'form_diff':   form_score(h_form) - form_score(a_form),
        'h_trend':     form_trend(h_form),
        'a_trend':     form_trend(a_form),
        'odds_fav':    odds_fav_val,
        'h_odd':       h_odd,
        'd_odd':       d_odd,
        'a_odd':       a_odd,
        'gg_odd':      gg_odd,
        'ov25_odd':    ov25,
        'ev_odd':      ev_odd,
    }


def build_fvecs(rounds):
    fvecs = []
    for rd in rounds:
        standings_map = {s['team_name']: s for s in rd.get('standings', [])}
        fv = {}
        for m in rd['matches']:
            feats = extract_match_features(m, standings_map)
            slot = m['n']
            for key in ('outcome', 'parity', 'cs', 'both_score', 'odds_fav',
                        'h_odd', 'a_odd', 'd_odd', 'pos_diff', 'pts_diff',
                        'form_diff', 'gg_odd', 'ov25_odd', 'ev_odd'):
                fv[f"M{slot}_{key}"] = discretize(feats.get(key), key)
        fvecs.append(fv)
    return fvecs


# ── Rule mining ───────────────────────────────────────────────────────────────

def mine_rules(fvecs, min_hits=4, min_precision=0.80):
    n = len(fvecs)
    all_keys = list(fvecs[0].keys())

    # Targets: every field of every slot in N+1
    target_keys = [k for k in all_keys]

    rules = []

    for target_key in target_keys:
        target_vals = [fvecs[i+1].get(target_key) for i in range(n-1)]

        # 1-feature
        for cond_key in all_keys:
            vc = defaultdict(Counter)
            for i in range(n-1):
                cv = fvecs[i].get(cond_key)
                tv = target_vals[i]
                if cv is not None and tv is not None:
                    vc[cv][tv] += 1
            for cv, tc in vc.items():
                total = sum(tc.values())
                if total < min_hits: continue
                for tv, hits in tc.items():
                    prec = hits / total
                    if prec >= min_precision:
                        recall = hits / max(1, sum(1 for v in target_vals if v == tv))
                        rules.append({'target': f"{target_key}={tv}",
                                      'conditions': {cond_key: cv},
                                      'hits': hits, 'total': total,
                                      'precision': round(prec, 3),
                                      'recall': round(recall, 3)})

        # 2-feature (focus on slots 5,6,7,10 as conditions)
        focus = [k for k in all_keys if any(k.startswith(f"M{s}_") for s in [5,6,7,10])]
        for k1, k2 in itertools.combinations(focus, 2):
            vc = defaultdict(Counter)
            for i in range(n-1):
                v1, v2, tv = fvecs[i].get(k1), fvecs[i].get(k2), target_vals[i]
                if v1 is not None and v2 is not None and tv is not None:
                    vc[(v1,v2)][tv] += 1
            for (v1,v2), tc in vc.items():
                total = sum(tc.values())
                if total < min_hits: continue
                for tv, hits in tc.items():
                    prec = hits / total
                    if prec >= min_precision:
                        recall = hits / max(1, sum(1 for v in target_vals if v == tv))
                        rules.append({'target': f"{target_key}={tv}",
                                      'conditions': {k1: v1, k2: v2},
                                      'hits': hits, 'total': total,
                                      'precision': round(prec, 3),
                                      'recall': round(recall, 3)})

    return sorted(rules, key=lambda r: (-r['precision'], -r['hits']))


def save_rules(rules, rounds_used):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rules")
            for r in rules:
                cur.execute("""
                    INSERT INTO rules (target, conditions, hits, total, precision, recall, discovered_at, rounds_used)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (r['target'], json.dumps(r['conditions']), r['hits'], r['total'],
                      r['precision'], r['recall'], datetime.now(), rounds_used))
        conn.commit()


if __name__ == '__main__':
    init_rules_table()
    print("VFL Learner started")

    while True:
        try:
            rounds = load_rounds()
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{ts}] {len(rounds)} rounds in DB")

            if len(rounds) < MIN_ROUNDS:
                print(f"  Waiting for {MIN_ROUNDS} rounds minimum...")
            else:
                fvecs = build_fvecs(rounds)
                rules = mine_rules(fvecs)
                print(f"  Found {len(rules)} rules")
                for r in rules[:10]:
                    print(f"  {r['precision']:.0%} ({r['hits']}/{r['total']}) | {r['target']} | IF {r['conditions']}")
                save_rules(rules, len(rounds))

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(RUN_EVERY)
