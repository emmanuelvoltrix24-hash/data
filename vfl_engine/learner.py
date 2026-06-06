"""VFL Learner — mines dimension rules from rounds data, persists to DB."""
import os, json, math, time
from collections import defaultdict, Counter
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from vfl_engine.stacker import DIMENSIONS
from vfl_engine.config.markets import SOURCES, get_bettable_targets, get_features

DB = os.environ.get("DATABASE_URL", "")


def get_conn():
    return psycopg2.connect(DB, cursor_factory=RealDictCursor)


def init_tables():
    """Create engine tables if not exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dim_rules (
                    id SERIAL PRIMARY KEY,
                    dimension TEXT NOT NULL,
                    dim_value TEXT NOT NULL,
                    condition_key TEXT NOT NULL,
                    condition_value TEXT NOT NULL,
                    lag INT DEFAULT 1,
                    hits FLOAT DEFAULT 0,
                    total FLOAT DEFAULT 0,
                    precision FLOAT DEFAULT 0,
                    source TEXT DEFAULT 'all',
                    slot TEXT,
                    discovered_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_dim_rules_source ON dim_rules(source)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_dim_rules_dim ON dim_rules(dimension)
            """)
            # Stacked predictions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stack_predictions (
                    id SERIAL PRIMARY KEY,
                    round_id TEXT,
                    source TEXT,
                    slot INT,
                    top3 JSONB,
                    derived JSONB,
                    top_prob FLOAT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_stack_pred_round ON stack_predictions(round_id, source)
            """)
        conn.commit()


def normalize_match(m, source):
    """Normalize a match to extract feature values."""
    hg = int(m.get('hg', m.get('home_goals', 0)) or 0)
    ag = int(m.get('ag', m.get('away_goals', 0)) or 0)
    total = hg + ag
    return {
        'hg': hg, 'ag': ag,
        'total': total,
        'parity': None if total == 0 else ('E' if total % 2 == 0 else 'O'),
        'outcome': 'W' if hg > ag else ('L' if hg < ag else 'D'),
        'gg_scored': hg > 0 and ag > 0,
        'tg25_scored': total >= 3,
        'cs_home': ag == 0 and hg > 0,
        'cs_away': hg == 0 and ag > 0,
    }


def extract_features(matches, source):
    """
    Extract feature vectors from a list of match dicts.
    Returns { slot_num: { feature: value } }
    """
    features = {}
    for m in matches:
        n = int(m.get('n', m.get('slot', 0)))
        norm = normalize_match(m, source)
        features[n] = {
            'M_parity': norm['parity'],
            'M_outcome': norm['outcome'],
            'M_gg_scored': norm['gg_scored'],
            'M_tg25_scored': norm['tg25_scored'],
            'M_cs_home': norm['cs_home'],
            'M_cs_away': norm['cs_away'],
            'M_hg': norm['hg'],
            'M_ag': norm['ag'],
            'M_total': norm['total'],
        }
    return features


def load_features_from_db(source=None, limit=500):
    """Load rounds from DB and extract features per slot."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT data FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s",
                    (source, limit))
            else:
                cur.execute(
                    "SELECT data FROM rounds ORDER BY round_id DESC LIMIT %s",
                    (limit,))
            rows = cur.fetchall()
    
    all_features = []
    for row in reversed(rows):
        data = row['data']
        if isinstance(data, str):
            data = json.loads(data)
        matches = data.get('matches', [])
        feats = extract_features(matches, source or data.get('source', ''))
        if feats:
            all_features.append(feats)
    return all_features


def mine_runs(all_features, source, min_hits=5, min_precision=0.75):
    """
    Mine dimension rules from a sequence of round feature dicts.
    For each dimension, find feature conditions that predict the next round's dimension value.
    """
    n = len(all_features)
    if n < 10:
        return []
    
    weights = [math.exp(-0.693 * (n - 1 - i) / 30) for i in range(n)]
    PRIOR_MASS = 3.6
    PRIOR_STR = 10
    
    rules = []
    
    for dim_name, dim in DIMENSIONS.items():
        feat_key = dim["feature"]
        dim_vals = dim["values"]
        
        # For each slot number
        slot_nums = set()
        for fv in all_features:
            slot_nums.update(fv.keys())
        
        for slot in sorted(slot_nums):
            for dim_val in dim_vals:
                # Collect all condition keys from previous round at this slot
                condition_counts = defaultdict(lambda: {'hits': 0.0, 'total': 0.0})
                
                for i in range(n - 1):
                    curr_slot = all_features[i].get(slot, {})
                    next_slot = all_features[i + 1].get(slot, {})
                    
                    curr_parity = curr_slot.get('M_parity')
                    curr_outcome = curr_slot.get('M_outcome')
                    next_val = next_slot.get(f'M_{feat_key}')
                    
                    if curr_parity is not None and next_val is not None:
                        ck = f'M{slot}_parity'
                        cv = str(curr_parity)
                        condition_counts[(ck, cv)]['total'] += weights[i]
                        if next_val == dim_val:
                            condition_counts[(ck, cv)]['hits'] += weights[i]
                    
                    if curr_outcome is not None and next_val is not None:
                        ck = f'M{slot}_outcome'
                        cv = str(curr_outcome)
                        condition_counts[(ck, cv)]['total'] += weights[i]
                        if next_val == dim_val:
                            condition_counts[(ck, cv)]['hits'] += weights[i]
                
                for (ck, cv), counts in condition_counts.items():
                    total = counts['total']
                    hits = counts['hits']
                    if total >= min_hits:
                        raw_prec = hits / total
                        bayes_prec = (hits + PRIOR_MASS) / (total + PRIOR_STR)
                        if bayes_prec >= min_precision:
                            rules.append({
                                'dimension': dim_name,
                                'dim_value': str(dim_val),
                                'condition_key': ck,
                                'condition_value': cv,
                                'lag': 1,
                                'hits': int(hits),
                                'total': int(total),
                                'precision': round(bayes_prec, 3),
                                'source': source or 'all',
                                'slot': str(slot),
                            })
        
        # Cross-slot rules (same round, different slots)
        for slot_a in sorted(slot_nums):
            for slot_b in sorted(slot_nums):
                if slot_a >= slot_b:
                    continue
                for dim_val in dim_vals:
                    counts = {'hits': 0.0, 'total': 0.0}
                    for i in range(n):
                        sa = all_features[i].get(slot_a, {})
                        sb = all_features[i].get(slot_b, {})
                        va = sa.get(f'M_{feat_key}')
                        vb = sb.get(f'M_{feat_key}')
                        if va is not None and vb is not None:
                            counts['total'] += 1
                            if vb == dim_val:
                                counts['hits'] += 1
                    
                    if counts['total'] >= min_hits:
                        raw_prec = counts['hits'] / counts['total']
                        bayes_prec = (counts['hits'] + PRIOR_MASS) / (counts['total'] + PRIOR_STR)
                        if bayes_prec >= min_precision:
                            rules.append({
                                'dimension': dim_name,
                                'dim_value': str(dim_val),
                                'condition_key': f'M{slot_a}_{feat_key}',
                                'condition_value': str(va),
                                'lag': 0,
                                'hits': int(counts['hits']),
                                'total': int(counts['total']),
                                'precision': round(bayes_prec, 3),
                                'source': source or 'all',
                                'slot': str(slot_b),
                            })
    
    return rules


def save_rules(rules, source):
    """Save mined dimension rules to DB (replace for source)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Delete old rules for this source
            cur.execute("DELETE FROM dim_rules WHERE source=%s", (source,))
            
            for r in rules:
                cur.execute("""
                    INSERT INTO dim_rules
                    (dimension, dim_value, condition_key, condition_value, lag,
                     hits, total, precision, source, slot, discovered_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (r['dimension'], r['dim_value'], r['condition_key'],
                      r['condition_value'], r['lag'],
                      r['hits'], r['total'], r['precision'],
                      r['source'], r['slot'], datetime.now()))
        conn.commit()


def load_rules(source=None):
    """Load mined rules from DB."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT * FROM dim_rules WHERE source=%s OR source='all' ORDER BY precision DESC LIMIT 500",
                    (source,))
            else:
                cur.execute(
                    "SELECT * FROM dim_rules ORDER BY precision DESC LIMIT 1000")
            rows = cur.fetchall()
    
    # Organize by dimension
    result = {}
    for r in rows:
        dim = r['dimension']
        if dim not in result:
            result[dim] = []
        result[dim].append({
            'condition': {r['condition_key']: r['condition_value']},
            'value': r['dim_value'],
            'precision': r['precision'],
            'hits': r['hits'],
            'total': r['total'],
            'slot': r['slot'],
        })
    return result


def run_cycle(source, limit=50):
    """One full learner cycle: load rounds → mine → save."""
    t0 = time.time()
    features = load_features_from_db(source, limit=limit)
    print(f"  {source}: {len(features)} rounds loaded", flush=True)
    
    if len(features) < 10:
        print(f"  {source}: too few rounds ({len(features)}), skipping", flush=True)
        return 0
    
    rules = mine_runs(features, source)
    print(f"  {source}: {len(rules)} dimension rules mined in {time.time()-t0:.0f}s", flush=True)
    
    if rules:
        save_rules(rules, source)
        print(f"  {source}: saved {len(rules)} rules", flush=True)
    
    return len(rules)
