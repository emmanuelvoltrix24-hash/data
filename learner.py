#!/usr/bin/env python3
"""
VFL Pattern Learner v2
- Multi-feature combos (1, 2, 3 features) across all fields
- Multi-lag windows: N→N+1, N-1→N+1, N-2→N+1
- Streak detection: consecutive same outcome/parity per slot
- Round-level derived features (total goals, draws, clean sheets, jump)
- Odds-implied probability buckets
- Confidence intervals: flag tentative rules (small sample)
- Rule decay: recent rounds weighted 2x more
- Anti-rules: conditions predicting what WON'T happen
- Rule conflict detection: flag contradicting rules for same target
- Failed rules log: degraded high-precision rules
- EV ranking: precision × hits
- Output grouped by target slot
"""
import os, json, time, itertools, logging
from collections import Counter, defaultdict
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

DATABASE_URL = os.environ['DATABASE_URL']
MIN_ROUNDS   = 30
RUN_EVERY    = 300


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    id SERIAL PRIMARY KEY,
                    target TEXT,
                    conditions JSONB,
                    lag INT,
                    hits INT,
                    total INT,
                    precision FLOAT,
                    recall FLOAT,
                    ev FLOAT,
                    discovered_at TIMESTAMP,
                    rounds_used INT,
                    status TEXT DEFAULT 'active'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS failed_rules (
                    id SERIAL PRIMARY KEY,
                    target TEXT,
                    conditions JSONB,
                    lag INT,
                    initial_precision FLOAT,
                    final_precision FLOAT,
                    initial_hits INT,
                    final_hits INT,
                    rounds_used INT,
                    failed_at TIMESTAMP
                )
            """)
        conn.commit()


def load_rounds():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rounds ORDER BY round_id ASC")
            rows = cur.fetchall()
            rounds = [r['data'] for r in rows]
            # Diagnostic: log the type of the first few rounds so we can
            # confirm whether JSONB is deserialised as dict or something else.
            for i, rd in enumerate(rounds[:3]):
                logging.info(
                    "load_rounds: rounds[%d] type=%s, is_dict=%s",
                    i, type(rd).__name__, isinstance(rd, dict),
                )
            return rounds


def load_previous_rules():
    """Load rules from last cycle for degradation tracking."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT target, conditions, lag, precision, hits FROM rules WHERE status='active'")
                return {(r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag']): r
                        for r in cur.fetchall()}
    except:
        return {}


# ── Statistical helpers ───────────────────────────────────────────────────────

def fisher_pvalue(hits, total, base_hits, base_total):
    """Two-tailed Fisher's exact test p-value (fast approximation)."""
    try:
        from math import comb, log
        # Hypergeometric: P(X >= hits) under null
        n11, n12 = hits, total - hits
        n21, n22 = base_hits - hits, base_total - total - (base_hits - hits)
        if any(x < 0 for x in [n11,n12,n21,n22]): return 1.0
        # Use scipy if available, else simple approximation
        try:
            from scipy.stats import fisher_exact
            _, p = fisher_exact([[n11,n12],[n21,n22]])
            return round(p, 4)
        except ImportError:
            # Approximation: chi-square
            n = n11+n12+n21+n22
            if n == 0: return 1.0
            e11 = (n11+n12)*(n11+n21)/n
            if e11 == 0: return 1.0
            chi2 = n * (n11*n22 - n12*n21)**2 / ((n11+n12)*(n21+n22)*(n11+n21)*(n12+n22))
            return round(min(1.0, 1/(1+chi2/3.84)), 4)  # rough p<0.05 proxy
    except:
        return 1.0


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

def streak(f):
    if not f: return 0, ''
    cur, count = f[0], 1
    for c in f[1:]:
        if c == cur: count += 1
        else: break
    return count, cur

def prob_bucket(odd):
    """Convert odds to implied probability bucket."""
    if odd is None: return None
    p = 1 / odd * 100
    if p >= 60: return '60+'
    if p >= 50: return '50-60'
    if p >= 40: return '40-50'
    if p >= 30: return '30-40'
    return '<30'


def discretize(val, key):
    if val is None: return None
    if key in ('h_odd', 'd_odd', 'a_odd', 'gg_odd', 'ov25_odd', 'ev_odd', 'dc1x_odd'):
        if val < 1.5: return 'vlow'
        if val < 2.0: return 'low'
        if val < 2.5: return 'med'
        if val < 3.5: return 'high'
        return 'vhigh'
    if key == 'pos_diff':
        if val <= -8: return 'H++'
        if val <= -3: return 'H+'
        if val <= 3:  return 'even'
        if val <= 8:  return 'A+'
        return 'A++'
    if key == 'pts_diff':
        if val >= 8:  return 'H++'
        if val >= 3:  return 'H+'
        if val >= -3: return 'even'
        if val >= -8: return 'A+'
        return 'A++'
    if key == 'form_diff':
        if val >= 6:  return 'H++'
        if val >= 2:  return 'H+'
        if val >= -2: return 'even'
        if val >= -6: return 'A+'
        return 'A++'
    if key == 'total':
        if val == 0: return '0'
        if val <= 2: return '1-2'
        if val <= 4: return '3-4'
        return '5+'
    if key == 'margin':
        if val == 0: return '0'
        if val == 1: return '1'
        if val == 2: return '2'
        return '3+'
    if key == 'hg':
        return str(min(val, 4))
    if key == 'ag':
        return str(min(val, 4))
    if key == 'h2h_hw':
        if val is None: return None
        if val >= 0.6: return 'H_dom'
        if val >= 0.4: return 'even'
        return 'A_dom'
    return val
    if val is None: return None
    if key in ('h_odd', 'd_odd', 'a_odd'):
        if val < 1.5: return 'vlow'
        if val < 2.0: return 'low'
        if val < 2.5: return 'med'
        if val < 3.5: return 'high'
        return 'vhigh'
    if key == 'pos_diff':
        if val <= -8: return 'H++'
        if val <= -3: return 'H+'
        if val <= 3:  return 'even'
        if val <= 8:  return 'A+'
        return 'A++'
    if key == 'pts_diff':
        if val >= 8:  return 'H++'
        if val >= 3:  return 'H+'
        if val >= -3: return 'even'
        if val >= -8: return 'A+'
        return 'A++'
    if key == 'form_diff':
        if val >= 6:  return 'H++'
        if val >= 2:  return 'H+'
        if val >= -2: return 'even'
        if val >= -6: return 'A+'
        return 'A++'
    if key in ('gg_odd', 'ov25_odd', 'ev_odd'):
        if val is None: return None
        if val < 1.6: return 'vlow'
        if val < 2.0: return 'low'
        if val < 2.5: return 'med'
        return 'high'
    if key == 'total':
        if val == 0: return '0'
        if val <= 2: return '1-2'
        if val <= 4: return '3-4'
        return '5+'
    if key == 'margin':
        if val == 0: return '0'
        if val == 1: return '1'
        if val == 2: return '2'
        return '3+'
    return val


def extract_match_features(m, standings_map):
    hg, ag = m['hg'], m['ag']
    hs  = standings_map.get(m['home_team'], {})
    as_ = standings_map.get(m['away_team'], {})
    h_form = hs.get('team_form', '')
    a_form = as_.get('team_form', '')
    h_pos  = hs.get('position', 10)
    a_pos  = as_.get('position', 10)

    mkts  = m.get('pre_markets', {})
    x2    = mkts.get('1X2', [])
    h_odd = float(x2[0]['odd_value']) if len(x2) > 0 else None
    d_odd = float(x2[1]['odd_value']) if len(x2) > 1 else None
    a_odd = float(x2[2]['odd_value']) if len(x2) > 2 else None

    gg    = mkts.get('GG', [])
    gg_odd = next((float(o['odd_value']) for o in gg if o.get('outcome_id')=='Yes'), None)

    tg25  = mkts.get('TG25', [])
    ov25  = next((float(o['odd_value']) for o in tg25 if 'over' in o.get('outcome_name','').lower()), None)

    tgoe  = mkts.get('TGOE', [])
    ev_odd = next((float(o['odd_value']) for o in tgoe if o.get('outcome_id')=='Even'), None)

    dc    = mkts.get('DC', [])
    dc1x  = next((float(o['odd_value']) for o in dc if o.get('outcome_id') in ('1X','12')), None)

    odds_fav = None
    if h_odd and d_odd and a_odd:
        if h_odd < a_odd and h_odd < d_odd: odds_fav = 'H'
        elif a_odd < h_odd and a_odd < d_odd: odds_fav = 'A'
        else: odds_fav = 'D'

    h_streak_n, h_streak_r = streak(h_form)
    a_streak_n, a_streak_r = streak(a_form)

    return {
        'outcome':      outcome(hg, ag),
        'parity':       par(hg, ag),
        'cs':           hg == 0 or ag == 0,
        'both_score':   hg > 0 and ag > 0,
        'total':        hg + ag,
        'margin':       abs(hg - ag),
        'hg':           hg,
        'ag':           ag,
        'pos_diff':     a_pos - h_pos,
        'pts_diff':     hs.get('points', 0) - as_.get('points', 0),
        'form_diff':    form_score(h_form) - form_score(a_form),
        'h_trend':      'up' if form_trend(h_form) > 0 else ('down' if form_trend(h_form) < 0 else 'flat'),
        'a_trend':      'up' if form_trend(a_form) > 0 else ('down' if form_trend(a_form) < 0 else 'flat'),
        'h_streak':     f"{h_streak_r}{h_streak_n}" if h_streak_n >= 2 else 'none',
        'a_streak':     f"{a_streak_r}{a_streak_n}" if a_streak_n >= 2 else 'none',
        'odds_fav':     odds_fav,
        'h_odd':        h_odd,
        'd_odd':        d_odd,
        'a_odd':        a_odd,
        'gg_odd':       gg_odd,
        'ov25_odd':     ov25,
        'ev_odd':       ev_odd,
        'dc1x_odd':     dc1x,
        'odds_agree':   odds_fav == ('H' if form_score(h_form) > form_score(a_form) else 'A') if odds_fav else None,
        'h_prob':       prob_bucket(h_odd),
        'a_prob':       prob_bucket(a_odd),
        'd_prob':       prob_bucket(d_odd),
    }


def build_fvecs(rounds):
    """Build feature vector per round including round-level, season-aware, and cross-round streak features."""
    fvecs = []
    prev_rid = None
    prev_season = None
    slot_history = defaultdict(list)  # slot -> last 3 outcomes
    season_round_counter = defaultdict(int)  # season_id -> round count within season
    h2h = defaultdict(list)  # (home, away) -> list of outcomes

    for idx, rd in enumerate(rounds):
        # Guard: rd must be a dict (JSONB from DB). If it's a tuple the row
        # factory didn't deserialise it — log the type and attempt recovery.
        if not isinstance(rd, dict):
            logging.warning(
                "build_fvecs: round[%d] has unexpected type %s (expected dict). "
                "Raw value (first 200 chars): %.200r",
                idx, type(rd).__name__, rd,
            )
            # Attempt 1: tuple of (key, value) pairs → dict
            if isinstance(rd, (tuple, list)):
                try:
                    rd = dict(rd)
                    logging.info("build_fvecs: round[%d] successfully coerced tuple→dict", idx)
                except (TypeError, ValueError):
                    pass
            # Attempt 2: JSON string stored instead of JSONB object
            if not isinstance(rd, dict) and isinstance(rd, str):
                try:
                    rd = json.loads(rd)
                    logging.info("build_fvecs: round[%d] successfully parsed JSON string→dict", idx)
                except (json.JSONDecodeError, TypeError):
                    pass
            # Still not a dict — skip this round entirely
            if not isinstance(rd, dict):
                logging.error(
                    "build_fvecs: round[%d] could not be converted to dict; skipping. "
                    "This round will NOT contribute to rule mining.",
                    idx,
                )
                continue

        standings_map = {s['team_name']: s for s in rd.get('standings', [])}
        fv = {}
        season_id = rd.get('season_id')

        for m in rd['matches']:
            feats = extract_match_features(m, standings_map)
            s = m['n']
            for key in ('outcome', 'parity', 'cs', 'both_score', 'odds_fav',
                        'h_odd', 'a_odd', 'd_odd', 'pos_diff', 'pts_diff',
                        'form_diff', 'h_trend', 'a_trend', 'h_streak', 'a_streak',
                        'gg_odd', 'ov25_odd', 'ev_odd', 'dc1x_odd', 'odds_agree',
                        'total', 'margin', 'hg', 'ag', 'h_prob', 'a_prob', 'd_prob'):
                fv[f"M{s}_{key}"] = discretize(feats.get(key), key)

            # Cross-round streak per slot
            hist = slot_history[s]
            if len(hist) >= 2:
                fv[f"M{s}_streak2"] = ''.join(hist[-2:])
            if len(hist) >= 3:
                fv[f"M{s}_streak3"] = ''.join(hist[-3:])
            slot_history[s].append(feats.get('outcome', '?'))
            if len(slot_history[s]) > 3:
                slot_history[s].pop(0)

            # H2H history for this fixture
            pair = (m['home_team'], m['away_team'])
            h2h_hist = h2h[pair]
            fv[f"M{s}_h2h_last"] = h2h_hist[-1] if h2h_hist else None
            fv[f"M{s}_h2h_hw"] = round(h2h_hist.count('W') / len(h2h_hist), 1) if h2h_hist else None
            h2h[pair].append(feats.get('outcome', '?'))
            if len(h2h[pair]) > 10:
                h2h[pair].pop(0)

        # Round-level derived features
        matches = rd['matches']
        totals  = [m['hg'] + m['ag'] for m in matches]
        fv['R_total_goals']  = discretize(sum(totals), 'total') if totals else None
        fv['R_total_parity'] = 'E' if sum(totals) % 2 == 0 else 'O'
        fv['R_draws']        = sum(1 for m in matches if m['hg'] == m['ag'])
        fv['R_cs']           = sum(1 for m in matches if m['hg'] == 0 or m['ag'] == 0)
        fv['R_home_wins']    = sum(1 for m in matches if m['hg'] > m['ag'])
        fv['R_away_wins']    = sum(1 for m in matches if m['ag'] > m['hg'])
        fv['R_draws_cat']    = '0' if fv['R_draws'] == 0 else ('1-2' if fv['R_draws'] <= 2 else '3+')
        fv['R_cs_cat']       = '0-2' if fv['R_cs'] <= 2 else ('3-5' if fv['R_cs'] <= 5 else '6+')

        # Jump
        rid = rd['round_id']
        if prev_rid:
            jump = rid - prev_rid
            fv['R_jump'] = 'normal' if jump <= 15 else ('skip' if jump <= 30 else 'break')
        else:
            fv['R_jump'] = 'normal'
        prev_rid = rid

        # Season-aware features
        fv['R_season_id']    = str(season_id) if season_id else None
        fv['R_season_start'] = rd.get('chain_break', False)
        fv['R_new_season']   = season_id != prev_season if prev_season else False

        if season_id:
            season_round_counter[season_id] += 1
            pos = season_round_counter[season_id]
            fv['R_season_pos'] = 'early' if pos <= 10 else ('mid' if pos <= 30 else 'late')
            fv['R_season_round'] = pos
        else:
            fv['R_season_pos'] = None
            fv['R_season_round'] = None

        prev_season = season_id
        fvecs.append(fv)
    return fvecs


# ── Rule mining ───────────────────────────────────────────────────────────────

def mine_rules(fvecs, min_hits=3, min_precision=None):
    n = len(fvecs)

    # Auto-threshold: lower precision requirement as dataset grows
    if min_precision is None:
        if n >= 500: min_precision = 0.72
        elif n >= 200: min_precision = 0.75
        elif n >= 100: min_precision = 0.78
        else: min_precision = 0.82

    all_keys = list(fvecs[0].keys())
    cond_keys_1feat = all_keys
    cond_keys_focus = [k for k in all_keys if any(
        k.startswith(f"M{s}_") for s in [5,6,7,10]
    ) or k.startswith('R_')]

    # Base rates for lift calculation
    base_rates = {}
    for tk in all_keys:
        vals = [fv.get(tk) for fv in fvecs if fv.get(tk) is not None]
        total = len(vals)
        if total:
            base_rates[tk] = Counter(vals)
            for v in base_rates[tk]:
                base_rates[tk][v] = base_rates[tk][v] / total

    rules = []

    for lag in [1, 2, 3]:
        pairs = [(i, i+lag) for i in range(n-lag)]
        # Split for stability check
        mid = len(pairs) // 2
        pairs_first  = pairs[:mid]
        pairs_second = pairs[mid:]

        for target_key in all_keys:
            target_vals = {j: fvecs[j].get(target_key) for _, j in pairs}
            all_tv = [v for v in target_vals.values() if v is not None]
            base_total = len(all_tv)

            def tgt(j): return target_vals.get(j)

            def make_rule(conditions, cv_tuple, tc, lag, key_names):
                total = sum(tc.values())
                if total < min_hits: return None
                for tv, hits in tc.items():
                    prec = hits / total
                    if prec < min_precision: continue
                    base_rate = base_rates.get(target_key, {}).get(tv, 0.5)
                    lift = round(prec / base_rate, 2) if base_rate > 0 else 1.0
                    recall = hits / max(1, all_tv.count(tv))
                    tentative = total < 8

                    # Stability: check precision on first vs second half
                    tc_first = Counter()
                    tc_second = Counter()
                    for pi, (i, j) in enumerate(pairs):
                        vals_match = all(fvecs[i].get(k) == v
                                         for k, v in zip(key_names, cv_tuple if isinstance(cv_tuple, tuple) else (cv_tuple,)))
                        tv2 = tgt(j)
                        if vals_match and tv2 is not None:
                            if pi < mid: tc_first[tv2] += 1
                            else: tc_second[tv2] += 1
                    p1 = tc_first[tv]/max(1,sum(tc_first.values())) if tc_first else 0
                    p2 = tc_second[tv]/max(1,sum(tc_second.values())) if tc_second else 0
                    stable = abs(p1 - p2) < 0.20

                    # Fisher p-value
                    base_hits = all_tv.count(tv)
                    pval = fisher_pvalue(hits, total, base_hits, base_total)

                    rules.append({
                        'target':     f"{target_key}={tv}",
                        'conditions': dict(zip(key_names, cv_tuple if isinstance(cv_tuple, tuple) else (cv_tuple,))),
                        'lag':        lag,
                        'hits':       hits, 'total': total,
                        'precision':  round(prec, 3),
                        'recall':     round(recall, 3),
                        'lift':       lift,
                        'pvalue':     pval,
                        'stable':     stable,
                        'tentative':  tentative or not stable or pval > 0.05,
                    })
                return None

            # 1-feature
            for ck in cond_keys_1feat:
                vc = defaultdict(Counter)
                for i, j in pairs:
                    cv, tv = fvecs[i].get(ck), tgt(j)
                    if cv is not None and tv is not None:
                        w = 2 if i >= n * 0.6 else 1
                        vc[cv][tv] += w
                for cv, tc in vc.items():
                    make_rule({ck: cv}, cv, tc, lag, [ck])

            # 2-feature
            for k1, k2 in itertools.combinations(cond_keys_focus, 2):
                vc = defaultdict(Counter)
                for i, j in pairs:
                    v1,v2,tv = fvecs[i].get(k1),fvecs[i].get(k2),tgt(j)
                    if None not in (v1,v2,tv):
                        w = 2 if i >= n * 0.6 else 1
                        vc[(v1,v2)][tv] += w
                for (v1,v2), tc in vc.items():
                    make_rule({k1:v1,k2:v2}, (v1,v2), tc, lag, [k1,k2])

    # Pre-select top focus keys by 1-feature signal strength (for 3-feat combos)
    key_scores = defaultdict(float)
    for r in rules:
        if len(r['conditions']) == 1:
            k = list(r['conditions'].keys())[0]
            key_scores[k] = max(key_scores[k], r['precision'] * r['hits'])
    top_focus = sorted(cond_keys_focus, key=lambda k: -key_scores.get(k, 0))[:15]

    # 3-feature (only top-15 focus keys)
    for lag in [1, 2, 3]:
        pairs = [(i, i+lag) for i in range(n-lag)]
        mid = len(pairs) // 2
        for target_key in all_keys:
            target_vals = {j: fvecs[j].get(target_key) for _, j in pairs}
            all_tv = [v for v in target_vals.values() if v is not None]
            def tgt(j): return target_vals.get(j)
            for k1,k2,k3 in itertools.combinations(top_focus, 3):
                vc = defaultdict(Counter)
                for i, j in pairs:
                    v1,v2,v3,tv = fvecs[i].get(k1),fvecs[i].get(k2),fvecs[i].get(k3),tgt(j)
                    if None not in (v1,v2,v3,tv):
                        w = 2 if i >= n * 0.6 else 1
                        vc[(v1,v2,v3)][tv] += w
                for combo, tc in vc.items():
                    make_rule({k1:combo[0],k2:combo[1],k3:combo[2]}, combo, tc, lag, [k1,k2,k3])

    # Deduplicate
                vc = defaultdict(Counter)
                for i, j in pairs:
                    v1,v2,v3,tv = fvecs[i].get(k1),fvecs[i].get(k2),fvecs[i].get(k3),tgt(j)
                    if None not in (v1,v2,v3,tv):
                        w = 2 if i >= n * 0.6 else 1
                        vc[(v1,v2,v3)][tv] += w
                for combo, tc in vc.items():
                    make_rule({k1:combo[0],k2:combo[1],k3:combo[2]}, combo, tc, lag, [k1,k2,k3])

        # Sequence rules: 3-round exact sequences per slot
        if lag == 1 and n >= 3:
            for slot in range(1, 11):
                ok = f"M{slot}_outcome"
                pk = f"M{slot}_parity"
                for prop in [ok, pk]:
                    seq_counts = defaultdict(Counter)
                    for i in range(n-2):
                        s1 = fvecs[i].get(prop)
                        s2 = fvecs[i+1].get(prop)
                        s3 = fvecs[i+2].get(prop) if i+2 < n else None
                        if None not in (s1, s2, s3):
                            seq_counts[(s1,s2)][s3] += 1
                    for (s1,s2), tc in seq_counts.items():
                        total = sum(tc.values())
                        if total < min_hits: continue
                        for s3, hits in tc.items():
                            prec = hits/total
                            if prec >= min_precision:
                                base_rate = base_rates.get(prop, {}).get(s3, 0.33)
                                rules.append({
                                    'target': f"{prop}={s3}",
                                    'conditions': {f"{prop}_seq": f"{s1}→{s2}"},
                                    'lag': 1, 'hits': hits, 'total': total,
                                    'precision': round(prec,3),
                                    'recall': round(hits/max(1,all_tv.count(s3) if (target_key:=prop) else 1),3),
                                    'lift': round(prec/base_rate,2) if base_rate else 1.0,
                                    'pvalue': 1.0, 'stable': True, 'tentative': total < 8,
                                })

    # Deduplicate
    seen = {}
    for r in rules:
        key = (r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag'])
        if key not in seen or r['precision'] > seen[key]['precision']:
            seen[key] = r
    rules = list(seen.values())

    # Negative sampling: log zero-predictive conditions
    zero_power = []
    for ck in cond_keys_focus[:20]:  # sample top focus keys
        for tv_key in all_keys[:10]:
            vals = Counter(fvecs[i].get(ck) for i in range(n) if fvecs[i].get(ck))
            for cv, cnt in vals.items():
                if cnt >= 10:
                    tv_vals = Counter(fvecs[i+1].get(tv_key) for i in range(n-1)
                                      if fvecs[i].get(ck)==cv and fvecs[i+1].get(tv_key))
                    if tv_vals:
                        max_prec = max(tv_vals.values()) / sum(tv_vals.values())
                        if max_prec < 0.35:
                            zero_power.append(f"{ck}={cv} → {tv_key} (max_prec={max_prec:.0%})")

    # Anti-rules
    anti = []
    for r in rules:
        tgt_key, tgt_val = r['target'].rsplit('=', 1)
        all_vals = set(fvecs[i].get(tgt_key) for i in range(n) if fvecs[i].get(tgt_key))
        for other_val in all_vals:
            if other_val != tgt_val:
                anti.append({**r, 'target': f"{tgt_key}!={other_val}", 'is_anti': True})
    rules += anti

    # Conflict detection
    cond_to_targets = defaultdict(list)
    for r in rules:
        if not r.get('is_anti'):
            ck = (json.dumps(r['conditions'], sort_keys=True), r['lag'])
            cond_to_targets[ck].append(r['target'])
    conflicts = {ck for ck, tgts in cond_to_targets.items() if len(set(tgts)) > 1}
    for r in rules:
        ck = (json.dumps(r['conditions'], sort_keys=True), r['lag'])
        r['conflict'] = ck in conflicts

    # Ensemble confidence: boost rules where 3+ independent rules agree
    target_rule_count = Counter(r['target'] for r in rules if not r.get('is_anti'))
    for r in rules:
        r['ensemble'] = target_rule_count.get(r['target'], 0)

    return sorted(rules, key=lambda r: (-r['precision'], -r.get('lift',1), -r['hits'])), zero_power


def compute_ev(rule):
    return round(rule['precision'] * rule['hits'], 2)


def chain_rules(rules, max_depth=2):
    """
    Chain rules: if A predicts X and B uses X as condition, produce A→B chain.
    Combined precision = precision_A × precision_B.
    Only chains non-anti, non-conflict rules with precision >= 0.80.
    """
    base = [r for r in rules if not r.get('is_anti') and not r.get('conflict')
            and r['precision'] >= 0.80]

    # Index rules by their condition key=value pairs
    # A rule's "output" is its target, e.g. "M10_outcome=W"
    # A rule's "input" is its conditions dict

    chained = []
    for r_a in base:
        tgt_a = r_a['target']  # e.g. "M10_outcome=W"
        tgt_key_a, tgt_val_a = tgt_a.rsplit('=', 1)

        # Find rules B where tgt_a appears as a condition
        for r_b in base:
            if r_b is r_a: continue
            conds_b = r_b['conditions']
            if tgt_key_a in conds_b and str(conds_b[tgt_key_a]) == tgt_val_a:
                # Chain: conditions of A → target of B
                combined_prec = round(r_a['precision'] * r_b['precision'], 3)
                if combined_prec < 0.65: continue  # too weak after chaining
                combined_conds = {**r_a['conditions']}  # A's conditions
                chained.append({
                    'target':     r_b['target'],
                    'conditions': combined_conds,
                    'lag':        r_a['lag'] + r_b['lag'],
                    'hits':       min(r_a['hits'], r_b['hits']),
                    'total':      min(r_a['total'], r_b['total']),
                    'precision':  combined_prec,
                    'recall':     round(r_a['recall'] * r_b['recall'], 3),
                    'tentative':  True,  # always tentative — derived
                    'conflict':   False,
                    'is_anti':    False,
                    'chained_via': tgt_a,
                })

    return chained


def save_rules(new_rules, prev_rules, rounds_used, zero_power):
    with get_db() as conn:
        with conn.cursor() as cur:
            # Rule versioning: update precision history for existing rules
            cur.execute("""
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS lift FLOAT;
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS pvalue FLOAT;
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS stable BOOLEAN;
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS ensemble INT;
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS chained_via TEXT;
                ALTER TABLE rules ADD COLUMN IF NOT EXISTS prec_history JSONB;
            """)

            # Detect failed/expired rules
            new_keys = {(r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag'])
                        for r in new_rules if not r.get('is_anti')}
            for key, prev in prev_rules.items():
                if key not in new_keys and prev['precision'] >= 0.85:
                    cur.execute("""
                        INSERT INTO failed_rules
                        (target, conditions, lag, initial_precision, final_precision,
                         initial_hits, final_hits, rounds_used, failed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (prev['target'], prev['conditions'], prev['lag'],
                          prev['precision'], 0.0, prev['hits'], 0,
                          rounds_used, datetime.now()))

            cur.execute("DELETE FROM rules")
            for r in new_rules:
                ev = compute_ev(r)
                # Build precision history from prev
                key = (r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag'])
                prev = prev_rules.get(key, {})
                hist = prev.get('prec_history', []) if isinstance(prev.get('prec_history'), list) else []
                hist = (hist + [r['precision']])[-10:]  # keep last 10

                cur.execute("""
                    INSERT INTO rules
                    (target, conditions, lag, hits, total, precision, recall, ev,
                     lift, pvalue, stable, ensemble, chained_via, prec_history,
                     discovered_at, rounds_used, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (r['target'], json.dumps(r['conditions']), r['lag'],
                      r['hits'], r['total'], r['precision'], r['recall'], ev,
                      r.get('lift', 1.0), r.get('pvalue', 1.0),
                      r.get('stable', True), r.get('ensemble', 0),
                      r.get('chained_via'), json.dumps(hist),
                      datetime.now(), rounds_used,
                      'tentative' if r.get('tentative') else 'active'))

            # Log zero-power conditions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS zero_power_log (
                    id SERIAL PRIMARY KEY,
                    description TEXT,
                    logged_at TIMESTAMP,
                    rounds_used INT
                )
            """)
            for desc in zero_power[:20]:
                cur.execute("INSERT INTO zero_power_log (description, logged_at, rounds_used) VALUES (%s,%s,%s)",
                            (desc, datetime.now(), rounds_used))
        conn.commit()


if __name__ == '__main__':
    init_tables()
    print("VFL Learner v2 started")

    while True:
        try:
            rounds = load_rounds()
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{ts}] {len(rounds)} rounds in DB")

            if len(rounds) < MIN_ROUNDS:
                print(f"  Need {MIN_ROUNDS} rounds minimum ({MIN_ROUNDS - len(rounds)} more)...")
            else:
                prev_rules = load_previous_rules()
                fvecs = build_fvecs(rounds)
                rules, zero_power = mine_rules(fvecs)
                chains = chain_rules(rules)
                rules = rules + chains
                save_rules(rules, prev_rules, len(rounds), zero_power)

                active  = [r for r in rules if not r.get('is_anti') and not r.get('conflict') and not r.get('chained_via')]
                chained_rules = [r for r in rules if r.get('chained_via')]
                anti    = [r for r in rules if r.get('is_anti')]
                conf    = [r for r in rules if r.get('conflict')]
                tent    = [r for r in rules if r.get('tentative') and not r.get('chained_via')]

                print(f"  Active: {len(active)}  Chained: {len(chained_rules)}  Anti: {len(anti)}  Conflicts: {len(conf)}  Tentative: {len(tent)}")

                # Group by target slot
                from collections import defaultdict as dd
                by_slot = dd(list)
                for r in active[:100]:
                    slot = r['target'].split('_')[0]
                    by_slot[slot].append(r)

                for slot in sorted(by_slot.keys()):
                    print(f"\n  [{slot}]")
                    for r in by_slot[slot][:3]:
                        flags = ('⚠️' if r.get('tentative') else '') + ('🔄' if r.get('conflict') else '')
                        print(f"    lag={r['lag']} {r['precision']:.0%} ({r['hits']}/{r['total']}) EV={compute_ev(r)} {flags} | {r['target']} | IF {r['conditions']}")

        except Exception as e:
            import traceback
            print(f"Error: {e}")
            traceback.print_exc()

        time.sleep(RUN_EVERY)
