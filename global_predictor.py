#!/usr/bin/env python3
"""
Global VFL Prediction Engine
Watches the rounds table for NEW completed rounds from ANY source.
Extracts features, matches DB rules, stores predictions.
"""
import os, sys, json, time, math
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

DB_URL = os.environ.get('DATABASE_URL', '')

# ── Feature extraction (normalized for all sources) ────────────────────────

def parity(total):
    return None if total == 0 else ('E' if total % 2 == 0 else 'O')

def bucket_odds(odd):
    if odd >= 6.0: return 'vhigh'
    if odd >= 3.5: return 'high'
    if odd >= 2.2: return 'med'
    if odd >= 1.6: return 'low'
    return 'vlow'

def normalize_matches(data, source, standings_dict=None):
    """Convert any source's match format to standard list."""
    raw_matches = data.get('matches', data.get('data', {}).get('matches', []))
    if not raw_matches:
        return []
    
    std = []
    for i, m in enumerate(raw_matches):
        n = m.get('n', i + 1)
        hg = int(m.get('hg', 0))
        ag = int(m.get('ag', 0))
        
        # Get team names (different field names per source)
        ht = m.get('home_team', m.get('home', ''))
        at = m.get('away_team', m.get('away', ''))
        
        result_str = m.get('result', m.get('score', f"{hg}:{ag}"))
        
        # Get odds
        h_odd = d_odd = a_odd = None
        pre_markets = m.get('pre_markets')
        odds = m.get('odds')
        
        if pre_markets and '1X2' in pre_markets:
            for o in pre_markets['1X2']:
                if o['outcome_id'] == '1': h_odd = float(o['odd_value'])
                if o['outcome_id'] == 'X': d_odd = float(o['odd_value'])
                if o['outcome_id'] == '2': a_odd = float(o['odd_value'])
        elif odds and '1x2' in odds:
            h_odd = odds['1x2'].get('1')
            d_odd = odds['1x2'].get('X')
            a_odd = odds['1x2'].get('2')
        
        # Get half time
        ht_hg = ht_ag = None
        ht_field = m.get('ht', '')
        if ht_field and ':' in str(ht_field):
            parts = str(ht_field).split(':')
            ht_hg = int(parts[0])
            ht_ag = int(parts[1])
        
        match = {
            'n': n, 'hg': hg, 'ag': ag,
            'home_team': ht, 'away_team': at,
            'result': result_str,
            'h_odd': h_odd, 'd_odd': d_odd, 'a_odd': a_odd,
            'ht_hg': ht_hg, 'ht_ag': ht_ag,
        }
        
        # Standings-based features
        if standings_dict:
            hs = standings_dict.get(ht, {})
            as_ = standings_dict.get(at, {})
            match['h_pos'] = hs.get('position', hs.get('pos'))
            match['a_pos'] = as_.get('position', as_.get('pos'))
            match['h_pts'] = hs.get('points', 0)
            match['a_pts'] = as_.get('points', 0)
            match['h_form'] = hs.get('team_form', '')
            match['a_form'] = as_.get('team_form', '')
        
        std.append(match)
    
    return std

def extract_features(matches, standings_dict=None):
    """Extract features from normalized matches."""
    # Track slot history across rounds for streaks (persisted across calls)
    if not hasattr(extract_features, "_slot_history"):
        extract_features._slot_history = {}
    _slot_history = extract_features._slot_history
    feat = {}
    for m in matches:
        n = m['n']
        hg, ag = m['hg'], m['ag']
        total = hg + ag
        ht = m.get('home_team', '')
        at = m.get('away_team', '')
        
        # Match-level features
        feat[f'M{n}_hg'] = hg
        feat[f'M{n}_ag'] = ag
        feat[f'M{n}_total'] = total
        feat[f'M{n}_parity'] = parity(total)
        feat[f'M{n}_outcome'] = 'W' if hg > ag else ('L' if hg < ag else 'D')
        feat[f'M{n}_cs'] = (hg == 0 or ag == 0)
        feat[f'M{n}_both_score'] = (hg > 0 and ag > 0)
        feat[f'M{n}_result'] = f"{hg}:{ag}"
        feat[f'M{n}_margin'] = hg - ag
        feat[f'M{n}_home_team'] = ht
        feat[f'M{n}_away_team'] = at
        
        # Late goals detection (learned from historical patterns)
        h_late = m.get('h_late_goals', 0) or 0
        a_late = m.get('a_late_goals', 0) or 0
        any_late = h_late > 0 or a_late > 0
        feat[f'M{n}_h_late_goals'] = h_late
        feat[f'M{n}_a_late_goals'] = a_late
        feat[f'M{n}_any_late'] = any_late
        feat[f'M{n}_both_late'] = (h_late > 0 and a_late > 0)
        
        # Market-friendly features (derived from actual score, always available)
        feat[f'M{n}_gg_yes'] = (hg > 0 and ag > 0)  # Both teams scored
        feat[f'M{n}_tg25_o'] = (total >= 3)  # Over 2.5 goals
        feat[f'M{n}_cs_home'] = (ag == 0 and hg > 0)  # Home clean sheet win
        feat[f'M{n}_cs_away'] = (hg == 0 and ag > 0)  # Away clean sheet win
        feat[f'M{n}_dc_home'] = (hg >= ag)  # Double chance home (1X)
        feat[f'M{n}_dc_away'] = (ag >= hg)  # Double chance away (X2)
        feat[f'M{n}_margin_group'] = 'big' if abs(hg-ag) >= 3 else ('small' if abs(hg-ag) == 1 else 'draw')
        if total >= 4: feat[f'M{n}_tg45_o'] = True
        elif total > 0: feat[f'M{n}_tg45_o'] = False
        # Exact score bands
        if total >= 5: feat[f'M{n}_score_band'] = '5plus'
        elif total == 4: feat[f'M{n}_score_band'] = '4'
        elif total == 3: feat[f'M{n}_score_band'] = '3'
        elif total == 2: feat[f'M{n}_score_band'] = '2'
        elif total == 1: feat[f'M{n}_score_band'] = '1'
        else: feat[f'M{n}_score_band'] = '0'
        
        # Half-time
        ht_hg = m.get('ht_hg')
        ht_ag = m.get('ht_ag')
        if ht_hg is not None and ht_ag is not None:
            ht_total = ht_hg + ht_ag
            feat[f'M{n}_ht_parity'] = parity(ht_total)
            feat[f'M{n}_ht_outcome'] = 'W' if ht_hg > ht_ag else ('L' if ht_hg < ht_ag else 'D')
            feat[f'M{n}_ht_hg'] = ht_hg
            feat[f'M{n}_ht_ag'] = ht_ag
        
        # Cross-round consistency — track slot history across rounds
        if n not in _slot_history:
            _slot_history[n] = []
        _slot_history[n].append(feat[f'M{n}_outcome'])
        if len(_slot_history[n]) >= 2:
            feat[f'M{n}_streak2'] = ''.join(_slot_history[n][-2:])
        if len(_slot_history[n]) >= 3:
            feat[f'M{n}_streak3'] = ''.join(_slot_history[n][-3:])
        
        # Odds
        h_odd = m.get('h_odd')
        d_odd = m.get('d_odd')
        a_odd = m.get('a_odd')
        if h_odd and d_odd and a_odd:
            h_prob = 1/h_odd * 100
            a_prob = 1/a_odd * 100
            feat[f'M{n}_h_odd'] = h_odd
            feat[f'M{n}_d_odd'] = d_odd
            feat[f'M{n}_a_odd'] = a_odd
            feat[f'M{n}_h_prob'] = round(h_prob, 1)
            feat[f'M{n}_a_prob'] = round(a_prob, 1)
            feat[f'M{n}_prob_diff'] = round(h_prob - a_prob, 1)
            feat[f'M{n}_h_odd_bucket'] = bucket_odds(h_odd)
            feat[f'M{n}_a_odd_bucket'] = bucket_odds(a_odd)
            feat[f'M{n}_odds_fav'] = 'H' if h_odd < a_odd else 'A'
        
        # Standings-based
        h_pos = m.get('h_pos')
        a_pos = m.get('a_pos')
        if h_pos is not None and a_pos is not None:
            pos_diff = int(a_pos) - int(h_pos)
            feat[f'M{n}_pos_diff'] = pos_diff
            if pos_diff >= 8: feat[f'M{n}_pos_diff_bucket'] = 'H++'
            elif pos_diff >= 4: feat[f'M{n}_pos_diff_bucket'] = 'H+'
            elif pos_diff >= -3: feat[f'M{n}_pos_diff_bucket'] = 'even'
            elif pos_diff >= -7: feat[f'M{n}_pos_diff_bucket'] = 'A+'
            else: feat[f'M{n}_pos_diff_bucket'] = 'A++'
        
        h_pts = m.get('h_pts', 0) or 0
        a_pts = m.get('a_pts', 0) or 0
        pts_diff = h_pts - a_pts
        feat[f'M{n}_pts_diff'] = pts_diff
        if pts_diff >= 15: feat[f'M{n}_pts_diff_bucket'] = 'H++'
        elif pts_diff >= 8: feat[f'M{n}_pts_diff_bucket'] = 'H+'
        elif pts_diff >= -7: feat[f'M{n}_pts_diff_bucket'] = 'even'
        elif pts_diff >= -14: feat[f'M{n}_pts_diff_bucket'] = 'A+'
        else: feat[f'M{n}_pts_diff_bucket'] = 'A++'
        
        # Form
        h_form = m.get('h_form', '')
        a_form = m.get('a_form', '')
        h_fs = h_form.count('W')*3 + h_form.count('D') if h_form else 0
        a_fs = a_form.count('W')*3 + a_form.count('D') if a_form else 0
        form_diff = h_fs - a_fs
        if form_diff >= 5: feat[f'M{n}_form_diff'] = 'H++'
        elif form_diff >= 2: feat[f'M{n}_form_diff'] = 'H+'
        elif form_diff >= -1: feat[f'M{n}_form_diff'] = 'even'
        elif form_diff >= -4: feat[f'M{n}_form_diff'] = 'A+'
        else: feat[f'M{n}_form_diff'] = 'A++'
        
        if len(h_form) >= 6:
            hr = h_form[:3].count('W')*3 + h_form[:3].count('D')
            ho = h_form[3:6].count('W')*3 + h_form[3:6].count('D')
            feat[f'M{n}_h_trend'] = 'up' if hr > ho else ('down' if hr < ho else 'flat')
        if len(a_form) >= 6:
            ar = a_form[:3].count('W')*3 + a_form[:3].count('D')
            ao = a_form[3:6].count('W')*3 + a_form[3:6].count('D')
            feat[f'M{n}_a_trend'] = 'up' if ar > ao else ('down' if ar < ao else 'flat')
        
        # Discretized odds buckets for feature matching
        h_odd = m.get('h_odd')
        a_odd = m.get('a_odd')
        if h_odd:
            feat[f'M{n}_h_prob'] = bucket_odds(h_odd)
        if a_odd:
            feat[f'M{n}_a_prob'] = bucket_odds(a_odd)
    
    # Round-level features (only if 10 matches)
    m_count = len(matches)
    if m_count == 10:
        outcomes = [feat.get(f'M{n}_outcome') for n in range(1, 11)]
        parities = [feat.get(f'M{n}_parity') for n in range(1, 11)]
        feat['R_home_wins'] = outcomes.count('W')
        feat['R_draws'] = outcomes.count('D')
        feat['R_away_wins'] = outcomes.count('L')
        o_count = parities.count('O')
        e_count = parities.count('E')
        feat['R_total_parity'] = 'O' if o_count > e_count else ('E' if e_count > o_count else 'even')
        feat['R_cs'] = sum(1 for n in range(1, 11) if feat.get(f'M{n}_cs'))
        feat['R_both_score'] = sum(1 for n in range(1, 11) if feat.get(f'M{n}_both_score'))
    
    return feat


# ── Rule matching ──────────────────────────────────────────────────────────

def match_rule(rule_conds, features):
    conditions = rule_conds if isinstance(rule_conds, dict) else json.loads(rule_conds)
    for k, v in conditions.items():
        fv = features.get(k)
        if fv is None:
            return False
        if str(fv) != str(v):
            return False
    return True

def predict_round(features, rules):
    """Match features against all rules. Returns list of {slot, target, prec, hits, total, lag, source, confidence}."""
    results = []
    matched_keys = set()
    
    for r in rules:
        conditions = r['conditions'] if isinstance(r['conditions'], dict) else json.loads(r['conditions'])
        if match_rule(conditions, features):
            target = r['target']
            prec = r['precision']
            hits = r['hits']
            total = r['total']
            
            # Extract slot from target
            slot = None
            for s in [f'M{n}' for n in range(1, 11)]:
                if target.startswith(f'{s}_') or target == s:
                    slot = s
                    break
            
            if not slot:
                continue
            
            # Determine confidence
            if prec >= 0.80 and total >= 50:
                conf = 'HIGH'
            elif prec >= 0.70 and total >= 30:
                conf = 'MEDIUM'
            elif prec >= 0.60 and total >= 20:
                conf = 'LOW'
            else:
                continue  # skip rules below 60%
            
            # Extract the predicted value and type (all market types the learner can generate)
            pred_val = None
            pred_type = None
            if '_outcome=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'outcome'
            elif '_ht_outcome=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'ht_outcome'
            elif '_parity=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'parity'
            elif '_ht_parity=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'ht_parity'
            elif '_total_parity=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'total_parity'
            elif '_cs=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'cs'
            elif '_ht_cs=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'ht_cs'
            elif '_both_score=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'both_score'
            elif '_both_late=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'both_late'
            elif '_any_late=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'any_late'
            elif '_gg_yes=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'gg_yes'
            elif '_tg25_o=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'tg25_o'
            elif '_odds_fav=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'odds_fav'
            elif '_h_trend=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'h_trend'
            elif '_a_trend=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'a_trend'
            elif '_h_prob=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'h_prob'
            elif '_a_prob=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'a_prob'
            elif '_form_diff=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'form_diff'
            elif '_pos_diff=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'pos_diff'
            elif '_pts_diff=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'pts_diff'
            elif '_streak2=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'streak2'
            elif '_streak3=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'streak3'
            elif 'R_total_parity=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'R_total_parity'
            elif 'R_draws=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'R_draws'
            elif 'R_cs=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'R_cs'
            elif 'R_home_wins=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'R_home_wins'
            elif '_gg_scored=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'gg_scored'
            elif '_tg25_scored=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'tg25_scored'
            elif '_tg45_scored=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'tg45_scored'
            elif '_cs_home=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'cs_home'
            elif '_cs_away=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'cs_away'
            elif '_dc_home=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'dc_home'
            elif '_dc_away=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'dc_away'
            elif '_margin_group=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'margin_group'
            elif '_score_band=' in target:
                pred_val = target.split('=')[1]
                pred_type = 'score_band'
            
            key = (slot, pred_type, pred_val)
            if key in matched_keys:
                continue  # only keep best rule per slot+prediction
            matched_keys.add(key)
            
            results.append({
                'slot': slot,
                'target': target,
                'pred_type': pred_type,
                'pred_val': pred_val,
                'precision': prec,
                'hits': hits,
                'total': total,
                'lag': r.get('lag', 1),
                'source': r.get('source', 'all'),
                'confidence': conf,
                'ev_score': round(prec * hits * math.log(total + 1, 10), 1)
            })
    
    # Sort by EV score
    results.sort(key=lambda x: -x['ev_score'])
    return results


# ── DB operations ──────────────────────────────────────────────────────────

_rules_cache = {'rules': None, 'loaded_at': 0}

def get_db():
    import psycopg2
    return psycopg2.connect(DB_URL)

def load_rules(min_prec=0.60, min_total=20, max_rules=15000):
    now = time.time()
    if now - _rules_cache['loaded_at'] < 300 and _rules_cache['rules']:
        return _rules_cache['rules']
    try:
        conn = get_db()
        cur = conn.cursor()
        # Load top rules per target type so new markets aren't drowned out by old ht_parity
        market_types = ['gg_scored','tg25_scored','tg45_scored','cs_home','cs_away',
                        'dc_home','dc_away','margin_group','score_band',
                        'outcome','cs','parity','ht_parity','total_parity',
                        'both_score','both_late','any_late','ht_outcome']
        rules = []
        for mt in market_types:
            like = f"%{mt}=%"
            cur.execute("""
                SELECT target, conditions::text, lag, precision, hits, total, source
                FROM global_rules
                WHERE status='active' AND precision >= %s AND total >= %s AND target LIKE %s
                ORDER BY (precision * hits * LOG(total+1)) DESC
                LIMIT 400
            """, (min_prec, min_total, like))
            for r in cur.fetchall():
                rules.append(r)
        # Fill remaining with top EV if under max_rules
        if len(rules) < max_rules:
            cur.execute("""
                SELECT target, conditions::text, lag, precision, hits, total, source
                FROM global_rules
                WHERE status='active' AND precision >= %s AND total >= %s
                ORDER BY (precision * hits * LOG(total+1)) DESC
                LIMIT %s
            """, (min_prec, min_total, max_rules - len(rules)))
            rules.extend(cur.fetchall())
        conn.close()
        loaded = []
        for r in rules:
            loaded.append({
                'target': r[0],
                'conditions': json.loads(r[1]),
                'lag': r[2],
                'precision': r[3],
                'hits': r[4],
                'total': r[5],
                'source': r[6],
            })
        _rules_cache['rules'] = loaded
        _rules_cache['loaded_at'] = time.time()
        return loaded
    except Exception as e:
        return _rules_cache.get('rules', [])

def get_latest_rounds():
    """Get latest round_id per source from rounds table."""
    conn = get_db()
    cur = conn.cursor()
    rounds = {}
    for src in ('betkraft', 'bongobongo', 'bangbet', 'betpawa'):
        try:
            # Different sources have different min matches. bangbet has 8, betkraft has 10.
            min_matches = 8 if src == 'bangbet' else 10
            cur.execute("""
                SELECT round_id::text, collected_at::text 
                FROM rounds WHERE source=%s 
                AND jsonb_array_length(data->'matches') >= %s
                ORDER BY collected_at DESC LIMIT 1
            """, (src, min_matches))
            row = cur.fetchone()
            if row:
                rounds[src] = {'round_id': row[0], 'collected_at': row[1]}
        except:
            pass
    conn.close()
    return rounds

def get_unpredicted_rounds(seen_ids):
    """Get rounds we haven't predicted yet."""
    if not seen_ids:
        return []
    conn = get_db()
    cur = conn.cursor()
    placeholders = ','.join(['%s'] * len(seen_ids))
    cur.execute(f"""
        SELECT id, round_id::text, source, data::text, collected_at::text
        FROM rounds
        WHERE (source, round_id::text) NOT IN ({placeholders})
        ORDER BY collected_at DESC
        LIMIT 5
    """, list(seen_ids))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_prediction(round_id, source, slot, target, pred_type, pred_val, 
                    precision, hits, total, lag, confidence, ev_score, features_json):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO predictions 
        (round_id, source, slot, target, pred_type, pred_val,
         precision, hits, total, lag, confidence, ev_score, features)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (round_id, source, slot, target) DO NOTHING
    """, (round_id, source, slot, target, pred_type, pred_val,
          precision, hits, total, lag, confidence, ev_score, features_json))
    conn.commit()
    conn.close()

def save_predictions_batch(predictions_list):
    conn = get_db()
    cur = conn.cursor()
    for p in predictions_list:
        try:
            cur.execute("""
                INSERT INTO predictions 
                (round_id, source, slot, target, pred_type, pred_val,
                 precision, hits, total, lag, confidence, ev_score, features)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (round_id, source, slot, target) DO NOTHING
            """, (p['round_id'], p['source'], p['slot'], p['target'],
                  p.get('pred_type'), p.get('pred_val'),
                  p['precision'], p['hits'], p['total'],
                  p['lag'], p['confidence'], p['ev_score'],
                  p['features']))
        except:
            pass
    conn.commit()
    conn.close()

def ensure_predictions_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            round_id TEXT NOT NULL,
            source TEXT NOT NULL,
            slot TEXT NOT NULL,
            target TEXT NOT NULL,
            pred_type TEXT,
            pred_val TEXT,
            precision FLOAT,
            hits INTEGER,
            total INTEGER,
            lag INTEGER DEFAULT 1,
            confidence TEXT,
            ev_score FLOAT,
            features JSONB,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(round_id, source, slot, target)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_round ON predictions(round_id, source)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at DESC)")
    conn.commit()
    conn.close()


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    print(f"🔮 Global VFL Predictor started [{datetime.now().strftime('%H:%M:%S')}]")
    
    ensure_predictions_table()
    rules = load_rules()
    print(f"   Loaded {len(rules)} rules from DB")
    
    # Track what we've predicted
    seen = set()  # (source, round_id)
    
    while True:
        try:
            # Refresh rules every 5 min
            if int(time.time()) % 300 < 3:
                rules = load_rules()
            
            # Get latest rounds per source
            latest = get_latest_rounds()
            
            for source, info in latest.items():
                rid = info['round_id']
                key = (source, rid)
                
                if key in seen:
                    continue
                
                # Fetch this round's data
                conn = get_db()
                cur = conn.cursor()
                cur.execute("""
                    SELECT data::text FROM rounds 
                    WHERE source=%s AND round_id::text=%s
                    LIMIT 1
                """, (source, rid))
                row = cur.fetchone()
                conn.close()
                
                if not row:
                    seen.add(key)
                    continue
                
                data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                
                # Get standings if available
                standings_dict = {}
                raw_standings = data.get('standings', [])
                for s in raw_standings:
                    team = s.get('team', s.get('team_name', ''))
                    if team:
                        standings_dict[team] = s
                
                # Normalize matches
                matches = normalize_matches(data, source, standings_dict)
                min_count = 8 if source == 'bangbet' else 10
                if len(matches) < min_count:
                    seen.add(key)
                    continue
                
                # Extract features
                features = extract_features(matches, standings_dict)
                if not features:
                    seen.add(key)
                    continue
                
                # Store source in features
                features['R_source'] = source
                
                # Match rules
                predictions = predict_round(features, rules)
                
                if predictions:
                    # Save best rule per slot per TYPE (outcome, ht_outcome, parity, ht_parity, total_parity, cs, ht_cs, both_score, both_late)
                    batch = []
                    best_per_slot = {}  # slot_type -> (ev_score, pred_dict)
                    for p in predictions:
                        st_key = (p['slot'], p.get('pred_type', ''))
                        if st_key not in best_per_slot or p['ev_score'] > best_per_slot[st_key][0]:
                            best_per_slot[st_key] = (p['ev_score'], p)
                    
                    for st_key, (score, p) in sorted(best_per_slot.items()):
                        batch.append({
                            'round_id': rid, 'source': source, 'slot': p['slot'],
                            'target': p['target'], 'pred_type': p.get('pred_type'),
                            'pred_val': p.get('pred_val'),
                            'precision': p['precision'], 'hits': p['hits'],
                            'total': p['total'], 'lag': p['lag'],
                            'confidence': p['confidence'], 'ev_score': p['ev_score'],
                            'features': json.dumps(features, default=str),
                        })
                    save_predictions_batch(batch)
                    
                    # Show pattern
                    p5 = features.get('M5_parity', '-')
                    p6 = features.get('M6_parity', '-')
                    p7 = features.get('M7_parity', '-')
                    p10 = features.get('M10_parity', '-')
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {source:>12} #{rid:<12} "
                          f"M5={p5} M6={p6} M7={p7} M10={p10} "
                          f"→ {len(batch)} predictions", flush=True)
                else:
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {source:>12} #{rid:<12} "
                          f"→ no matching rules", flush=True)
                
                seen.add(key)
            
            time.sleep(3)
            
        except KeyboardInterrupt:
            print("\n🛑 Stopped")
            break
        except Exception as e:
            print(f"⚠️ {e}", flush=True)
            time.sleep(10)


if __name__ == '__main__':
    main()
