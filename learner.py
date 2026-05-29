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

DATABASE_URL = os.environ.get('DATABASE_URL', '')
MIN_ROUNDS   = 20
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
    if key in ('h_odd', 'd_odd', 'a_odd', 'gg_yes', 'gg_no', 'tg15_o', 'tg15_u',
               'tg25_o', 'tg25_u', 'tg35_o', 'tg35_u', 'tgoe_e', 'tgoe_o',
               'dc_1x', 'dc_12', 'dc_x2',
               't1g_y', 't1g_n', 't2g_y', 't2g_n',
               't1ou15_o', 't1ou15_u', 't2ou15_o', 't2ou15_u',
               'hgg_y', 'hgg_n',
               'h1x2_h', 'h1x2_d', 'h1x2_a', 'dch_1x', 'dch_12', 'dch_x2',
               'tfg_0', 'tfg_15', 'tfg_30',
               'dr_hh', 'dr_dd', 'dr_aa',
               'x2g_1g', 'x2g_1ng', 'x2g_2g', 'x2g_2ng', 'x2g_xg', 'x2g_xng',
               'x2ou15_1o', 'x2ou15_1u', 'x2ou15_2o', 'x2ou25_1o', 'x2ou25_2o'):
        if val < 1.5: return 'vlow'
        if val < 2.0: return 'low'
        if val < 2.5: return 'med'
        if val < 3.5: return 'high'
        if val < 5.0: return 'vhigh'
        return 'extreme'
    # Correct Score / HT Score / TG exact: raw decimal odds
    if key in ('cs_00', 'cs_11', 'cs_10', 'cs_21', 'cs_01', 'cs_12',
               'hs_00', 'hs_10', 'hs_01', 'hs_11',
               'tg_0', 'tg_1', 'tg_2', 'tg_3', 'tg_4',
               'fts_h', 'fts_a', 'fts_0',
               'mg_02', 'mg_13', 'mg_24', 'mg_4p'):
        if val < 5: return 'likely'
        if val < 10: return 'possible'
        if val < 20: return 'unlikely'
        return 'longshot'
    if key == 'overround':
        if val < 0.05: return 'tight'
        if val < 0.10: return 'normal'
        return 'wide'
    if key == 'consensus':
        return val if val in ('high', 'med', 'low') else None
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


def _safe_odd(mkts, market, condition_key, condition_val, fallback_key=None):
    """Extract a single odd value from any market list, safely."""
    try:
        items = mkts.get(market, [])
        if not items or not isinstance(items, list):
            return None
        for o in items:
            if isinstance(o, dict):
                if o.get(condition_key) == condition_val:
                    return float(o['odd_value'])
    except (KeyError, ValueError, TypeError, IndexError):
        pass
    return None


def _safe_h2h_odd(mkts, market, outcome_id):
    """Extract H2H (1X2 combo) odd. Handles different id formats."""
    return _safe_odd(mkts, market, 'outcome_id', outcome_id)


def _safe_ou_odd(mkts, market, over_or_under):
    """Extract Over/Under odd from O/U markets."""
    try:
        items = mkts.get(market, [])
        if not items or not isinstance(items, list):
            return None
        for o in items:
            if isinstance(o, dict):
                name = o.get('outcome_name', '').lower()
                oid = o.get('outcome_id', '').lower()
                if over_or_under.lower() in name or over_or_under.lower() in oid:
                    return float(o['odd_value'])
    except (KeyError, ValueError, TypeError):
        pass
    return None


def _team_goal_odd(mkts, market, yes_or_no):
    """Extract Team Goal/No Goal odds."""
    return _safe_odd(mkts, market, 'outcome_id', yes_or_no)


def _ht_score_parity(ht_str):
    """Parse HT score string like '1:0' and return parity (E/O) and outcome (W/D/L)."""
    if not ht_str or not isinstance(ht_str, str) or ':' not in ht_str:
        return None, None
    try:
        h, a = ht_str.split(':')
        hg_ht, ag_ht = int(h), int(a)
        t = hg_ht + ag_ht
        parity = None if t == 0 else ('E' if t % 2 == 0 else 'O')
        outcome = 'W' if hg_ht > ag_ht else ('L' if hg_ht < ag_ht else 'D')
        return parity, outcome
    except (ValueError, TypeError):
        return None, None


def _goal_time_features(score_times):
    """Classify goal times into early/late buckets. Returns dict."""
    if not score_times or not isinstance(score_times, list):
        return {'early': 0, 'mid': 0, 'late': 0, 'any': False}
    early = sum(1 for t in score_times if t <= 30)
    mid = sum(1 for t in score_times if 30 < t <= 60)
    late = sum(1 for t in score_times if t > 60)
    return {'early': early, 'mid': mid, 'late': late, 'any': len(score_times) > 0}


def extract_match_features(m, standings_map, running_slot_stats=None):
    hg, ag = m['hg'], m['ag']
    hs  = standings_map.get(m['home_team'], {})
    as_ = standings_map.get(m['away_team'], {})
    h_form = hs.get('team_form', '')
    a_form = as_.get('team_form', '')
    h_pos  = hs.get('position', 10)
    a_pos  = as_.get('position', 10)

    mkts  = m.get('pre_markets', {})

    # === HT features from ht field ===
    ht_str = m.get('ht')
    ht_parity, ht_outcome = _ht_score_parity(ht_str)
    ht_hg, ht_ag = None, None
    if ht_str and ':' in str(ht_str):
        try:
            parts = str(ht_str).split(':')
            ht_hg, ht_ag = int(parts[0]), int(parts[1])
        except: pass
    ht_cs = (ht_hg == 0 or ht_ag == 0) if (ht_hg is not None) else None
    ht_both = (ht_hg > 0 and ht_ag > 0) if (ht_hg is not None) else None

    # HT→FT transition: e.g. 'HH', 'HD', 'HA', 'DH', 'DD', etc
    ht_ft_trans = None
    if ht_outcome and outcome(hg, ag):
        ht_ft_trans = ht_outcome + outcome(hg, ag)

    # === Goal timing features ===
    h_gt = _goal_time_features(m.get('home_score_times'))
    a_gt = _goal_time_features(m.get('away_score_times'))
    both_late = h_gt['late'] > 0 and a_gt['late'] > 0  # both scored late
    any_late = h_gt['late'] > 0 or a_gt['late'] > 0
    early_goal = h_gt['early'] > 0 or a_gt['early'] > 0

    # === Core odds ===
    x2    = mkts.get('1X2', [])
    h_odd = float(x2[0]['odd_value']) if len(x2) > 0 else None
    d_odd = float(x2[1]['odd_value']) if len(x2) > 1 else None
    a_odd = float(x2[2]['odd_value']) if len(x2) > 2 else None

    odds_fav = None
    if h_odd and d_odd and a_odd:
        if h_odd < a_odd and h_odd < d_odd: odds_fav = 'H'
        elif a_odd < h_odd and a_odd < d_odd: odds_fav = 'A'
        else: odds_fav = 'D'

    # === Value bet (implied prob vs market) ===
    overround = None
    if h_odd and d_odd and a_odd:
        implied = 1/h_odd + 1/d_odd + 1/a_odd
        overround = round(implied - 1, 3)

    # === All markets — extracted safely ===
    # Goal-line markets
    gg_yes  = _safe_odd(mkts, 'GG', 'outcome_id', 'Y')
    gg_no   = _safe_odd(mkts, 'GG', 'outcome_id', 'N')
    tg15_o  = _safe_ou_odd(mkts, 'TG15', 'Over')
    tg15_u  = _safe_ou_odd(mkts, 'TG15', 'Under')
    tg25_o  = _safe_ou_odd(mkts, 'TG25', 'Over')
    tg25_u  = _safe_ou_odd(mkts, 'TG25', 'Under')
    tg35_o  = _safe_ou_odd(mkts, 'TG35', 'Over')
    tg35_u  = _safe_ou_odd(mkts, 'TG35', 'Under')

    # Exact goals markets
    cs_00 = _safe_odd(mkts, 'CS', 'outcome_id', '0-0')
    cs_11 = _safe_odd(mkts, 'CS', 'outcome_id', '1-1')
    cs_22 = _safe_odd(mkts, 'CS', 'outcome_id', '2-2')
    cs_10 = _safe_odd(mkts, 'CS', 'outcome_id', '1-0')
    cs_21 = _safe_odd(mkts, 'CS', 'outcome_id', '2-1')
    cs_01 = _safe_odd(mkts, 'CS', 'outcome_id', '0-1')
    cs_12 = _safe_odd(mkts, 'CS', 'outcome_id', '1-2')

    # TG exact goal count
    tg_0 = _safe_odd(mkts, 'TG', 'outcome_id', '0')
    tg_1 = _safe_odd(mkts, 'TG', 'outcome_id', '1')
    tg_2 = _safe_odd(mkts, 'TG', 'outcome_id', '2')
    tg_3 = _safe_odd(mkts, 'TG', 'outcome_id', '3')
    tg_4 = _safe_odd(mkts, 'TG', 'outcome_id', '4')

    # Multi-goal (range)
    mg_02 = _safe_odd(mkts, 'MG', 'outcome_id', '0-2')
    mg_13 = _safe_odd(mkts, 'MG', 'outcome_id', '1-3')
    mg_24 = _safe_odd(mkts, 'MG', 'outcome_id', '2-4')
    mg_4p = _safe_odd(mkts, 'MG', 'outcome_id', '>4')

    # Team-specific goal markets
    t1g_y = _safe_odd(mkts, 'T1G', 'outcome_id', 'Y')
    t1g_n = _safe_odd(mkts, 'T1G', 'outcome_id', 'N')
    t2g_y = _safe_odd(mkts, 'T2G', 'outcome_id', 'Y')
    t2g_n = _safe_odd(mkts, 'T2G', 'outcome_id', 'N')
    t1ou15_o = _safe_odd(mkts, 'T1OU15', 'outcome_id', 'O')
    t1ou15_u = _safe_odd(mkts, 'T1OU15', 'outcome_id', 'U')
    t2ou15_o = _safe_odd(mkts, 'T2OU15', 'outcome_id', 'O')
    t2ou15_u = _safe_odd(mkts, 'T2OU15', 'outcome_id', 'U')

    # First team to score
    fts_h = _safe_odd(mkts, 'FTS', 'outcome_id', 'H')
    fts_a = _safe_odd(mkts, 'FTS', 'outcome_id', 'A')
    fts_0 = _safe_odd(mkts, 'FTS', 'outcome_id', '0')

    # HT-specific markets
    hgg_y = _safe_odd(mkts, 'HGG', 'outcome_id', 'Y')
    hgg_n = _safe_odd(mkts, 'HGG', 'outcome_id', 'N')
    hs_00 = _safe_odd(mkts, 'HS', 'outcome_id', '0-0')
    hs_10 = _safe_odd(mkts, 'HS', 'outcome_id', '1-0')
    hs_01 = _safe_odd(mkts, 'HS', 'outcome_id', '0-1')
    hs_11 = _safe_odd(mkts, 'HS', 'outcome_id', '1-1')
    h1x2_h = _safe_odd(mkts, 'H1X2', 'outcome_id', '1')
    h1x2_d = _safe_odd(mkts, 'H1X2', 'outcome_id', 'X')
    h1x2_a = _safe_odd(mkts, 'H1X2', 'outcome_id', '2')
    dch_1x = _safe_odd(mkts, 'DCH', 'outcome_id', '1X')
    dch_12 = _safe_odd(mkts, 'DCH', 'outcome_id', '12')
    dch_x2 = _safe_odd(mkts, 'DCH', 'outcome_id', 'X2')

    # Time of first goal
    tfg_0  = _safe_odd(mkts, 'TFG', 'outcome_id', '0')
    tfg_15 = _safe_odd(mkts, 'TFG', 'outcome_id', '1')
    tfg_30 = _safe_odd(mkts, 'TFG', 'outcome_id', '2')

    # DC and DR
    dc_1x = _safe_odd(mkts, 'DC', 'outcome_id', '1X')
    dc_12 = _safe_odd(mkts, 'DC', 'outcome_id', '12')
    dc_x2 = _safe_odd(mkts, 'DC', 'outcome_id', 'X2')
    dr_hh = _safe_odd(mkts, 'DR', 'outcome_id', 'HH')
    dr_dd = _safe_odd(mkts, 'DR', 'outcome_id', 'DD')
    dr_aa = _safe_odd(mkts, 'DR', 'outcome_id', 'AA')

    # TGOE
    tgoe_e = _safe_odd(mkts, 'TGOE', 'outcome_id', 'E')
    tgoe_o = _safe_odd(mkts, 'TGOE', 'outcome_id', 'O')

    # 1X2G (1X2 + BTTS combo)
    x2g_1g  = _safe_odd(mkts, '1X2G', 'outcome_id', '1G')
    x2g_1ng = _safe_odd(mkts, '1X2G', 'outcome_id', '1NG')
    x2g_2g  = _safe_odd(mkts, '1X2G', 'outcome_id', '2G')
    x2g_2ng = _safe_odd(mkts, '1X2G', 'outcome_id', '2NG')
    x2g_xg  = _safe_odd(mkts, '1X2G', 'outcome_id', 'XG')
    x2g_xng = _safe_odd(mkts, '1X2G', 'outcome_id', 'XNG')

    # 1X2+OU combos (15, 25, 35, 45, 55)
    x2ou15_1o = _safe_odd(mkts, '1X2OU15', 'outcome_id', '1O')
    x2ou15_1u = _safe_odd(mkts, '1X2OU15', 'outcome_id', '1U')
    x2ou15_2o = _safe_odd(mkts, '1X2OU15', 'outcome_id', '2O')
    x2ou25_1o = _safe_odd(mkts, '1X2OU25', 'outcome_id', '1O')
    x2ou25_2o = _safe_odd(mkts, '1X2OU25', 'outcome_id', '2O')

    # === Form features ===
    h_streak_n, h_streak_r = streak(h_form)
    a_streak_n, a_streak_r = streak(a_form)

    # === Running averages per slot (if slot_stats provided) ===
    s = m['n']
    running_goals = None
    running_total = None
    if running_slot_stats is not None and s in running_slot_stats:
        stats = running_slot_stats[s]
        if stats.get('count', 0) >= 2:
            running_goals = round(stats.get('avg_goals', 0), 1)
            running_total = stats.get('count', 0)
            running_goals_cat = '0' if running_goals == 0 else ('low' if running_goals < 2 else ('med' if running_goals < 3 else 'high'))
        else:
            running_goals_cat = None
    else:
        running_goals_cat = None

    # === Cross-market consensus score ===
    # How many markets agree with the odds favourite
    consensus = 0
    if odds_fav == 'H':
        if tg25_o and tg25_o < 2.0: consensus += 1  # Over 2.5 odds low = high scoring expected
        if gg_yes and gg_yes < 1.8: consensus += 1  # BTTS Yes likely
        if h1x2_h and h1x2_h < 2.5: consensus += 1  # HT home win likely
        if fts_h and fts_h < 2.0: consensus += 1     # FT home to score first
    elif odds_fav == 'A':
        if h1x2_a and h1x2_a < 3.0: consensus += 1
        if fts_a and fts_a < 2.5: consensus += 1
    consensus_score = 'high' if consensus >= 3 else ('med' if consensus >= 1 else 'low')

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
        # Core odds
        'odds_fav':     odds_fav,
        'h_odd':        h_odd,
        'd_odd':        d_odd,
        'a_odd':        a_odd,
        'overround':    overround,
        'h_prob':       prob_bucket(h_odd),
        'a_prob':       prob_bucket(a_odd),
        'd_prob':       prob_bucket(d_odd),
        # Betting markets
        'gg_yes':       gg_yes,
        'gg_no':        gg_no,
        'tg15_o':       tg15_o,
        'tg15_u':       tg15_u,
        'tg25_o':       tg25_o,
        'tg25_u':       tg25_u,
        'tg35_o':       tg35_o,
        'tg35_u':       tg35_u,
        'tgoe_e':       tgoe_e,
        'tgoe_o':       tgoe_o,
        'dc_1x':        dc_1x,
        'dc_12':        dc_12,
        'dc_x2':        dc_x2,
        # Correct Score
        'cs_00':        cs_00,
        'cs_11':        cs_11,
        'cs_22':        cs_22,
        'cs_10':        cs_10,
        'cs_21':        cs_21,
        'cs_01':        cs_01,
        'cs_12':        cs_12,
        # TG exact
        'tg_0':         tg_0,
        'tg_1':         tg_1,
        'tg_2':         tg_2,
        'tg_3':         tg_3,
        'tg_4':         tg_4,
        # MG ranges
        'mg_02':        mg_02,
        'mg_13':        mg_13,
        'mg_24':        mg_24,
        'mg_4p':        mg_4p,
        # Team goal markets
        't1g_y':        t1g_y,
        't1g_n':        t1g_n,
        't2g_y':        t2g_y,
        't2g_n':        t2g_n,
        't1ou15_o':     t1ou15_o,
        't1ou15_u':     t1ou15_u,
        't2ou15_o':     t2ou15_o,
        't2ou15_u':     t2ou15_u,
        # FTS
        'fts_h':        fts_h,
        'fts_a':        fts_a,
        'fts_0':        fts_0,
        # HT markets
        'hgg_y':        hgg_y,
        'hgg_n':        hgg_n,
        'hs_00':        hs_00,
        'hs_10':        hs_10,
        'hs_01':        hs_01,
        'hs_11':        hs_11,
        'h1x2_h':       h1x2_h,
        'h1x2_d':       h1x2_d,
        'h1x2_a':       h1x2_a,
        'dch_1x':       dch_1x,
        'dch_12':       dch_12,
        'dch_x2':       dch_x2,
        # TFG
        'tfg_0':        tfg_0,
        'tfg_15':       tfg_15,
        'tfg_30':       tfg_30,
        # DR (HT/FT)
        'dr_hh':        dr_hh,
        'dr_dd':        dr_dd,
        'dr_aa':        dr_aa,
        # 1X2G combos
        'x2g_1g':       x2g_1g,
        'x2g_1ng':      x2g_1ng,
        'x2g_2g':       x2g_2g,
        'x2g_2ng':      x2g_2ng,
        'x2g_xg':       x2g_xg,
        'x2g_xng':      x2g_xng,
        # 1X2+OU combos
        'x2ou15_1o':    x2ou15_1o,
        'x2ou15_1u':    x2ou15_1u,
        'x2ou15_2o':    x2ou15_2o,
        'x2ou25_1o':    x2ou25_1o,
        'x2ou25_2o':    x2ou25_2o,
        # Odds agreement / consensus
        'odds_agree':   odds_fav == ('H' if form_score(h_form) > form_score(a_form) else 'A') if odds_fav else None,
        'consensus':    consensus_score,
        # HT features
        'ht_parity':    ht_parity,
        'ht_outcome':   ht_outcome,
        'ht_cs':        ht_cs,
        'ht_both':      ht_both,
        'ht_ft_trans':  ht_ft_trans,
        # Goal timing features
        'h_early_goals': h_gt['early'],
        'h_late_goals':  h_gt['late'],
        'a_early_goals': a_gt['early'],
        'a_late_goals':  a_gt['late'],
        'both_late':     both_late,
        'any_late':      any_late,
        'early_goal':    early_goal,
        # Running averages
        'running_goals':  running_goals_cat,
    }


def build_fvecs(rounds):
    """Build feature vector per round including round-level, season-aware, and cross-round streak features."""
    fvecs = []
    prev_rid = None
    prev_season = None
    slot_history = defaultdict(list)  # slot -> last 3 outcomes
    slot_goal_history = defaultdict(list)  # slot -> last 5 total goals
    season_round_counter = defaultdict(int)  # season_id -> round count within season
    h2h = defaultdict(list)  # (home, away) -> list of outcomes
    _seen_home_sigs = set()  # track repeated home team sets
    team_goal_history = defaultdict(list)  # team_name -> last 10 goals scored

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

        # Build running slot stats before extracting features
        running_slot_stats = {}
        for s in range(1, 11):
            if slot_goal_history[s]:
                running_slot_stats[s] = {
                    'avg_goals': sum(slot_goal_history[s]) / len(slot_goal_history[s]),
                    'count': len(slot_goal_history[s]),
                }

        for m in rd['matches']:
            feats = extract_match_features(m, standings_map, running_slot_stats)
            s = m['n']
            for key in ('outcome', 'parity', 'cs', 'both_score', 'odds_fav',
                        'h_odd', 'a_odd', 'd_odd', 'pos_diff', 'pts_diff',
                        'form_diff', 'h_trend', 'a_trend', 'h_streak', 'a_streak',
                        'odds_agree', 'consensus',
                        'total', 'margin', 'hg', 'ag', 'h_prob', 'a_prob', 'd_prob',
                        'overround',
                        'gg_yes', 'gg_no', 'tg15_o', 'tg15_u', 'tg25_o', 'tg25_u', 'tg35_o', 'tg35_u',
                        'tgoe_e', 'tgoe_o', 'dc_1x', 'dc_12', 'dc_x2',
                        'cs_00', 'cs_11', 'cs_10', 'cs_21', 'cs_01', 'cs_12',
                        'tg_0', 'tg_1', 'tg_2', 'tg_3', 'tg_4',
                        'mg_02', 'mg_13', 'mg_24', 'mg_4p',
                        't1g_y', 't1g_n', 't2g_y', 't2g_n',
                        't1ou15_o', 't1ou15_u', 't2ou15_o', 't2ou15_u',
                        'fts_h', 'fts_a', 'fts_0',
                        'hgg_y', 'hgg_n', 'hs_00', 'hs_10', 'hs_01', 'hs_11',
                        'h1x2_h', 'h1x2_d', 'h1x2_a', 'dch_1x', 'dch_12', 'dch_x2',
                        'tfg_0', 'tfg_15', 'tfg_30',
                        'dr_hh', 'dr_dd', 'dr_aa',
                        'x2g_1g', 'x2g_1ng', 'x2g_2g', 'x2g_2ng', 'x2g_xg', 'x2g_xng',
                        'x2ou15_1o', 'x2ou15_1u', 'x2ou15_2o', 'x2ou25_1o', 'x2ou25_2o',
                        'ht_parity', 'ht_outcome', 'ht_cs', 'ht_both', 'ht_ft_trans',
                        'h_early_goals', 'h_late_goals',
                        'a_early_goals', 'a_late_goals',
                        'both_late', 'any_late', 'early_goal',
                        'running_goals'):
                val = feats.get(key)
                if val is not None:
                    fv[f"M{s}_{key}"] = discretize(val, key)
                else:
                    fv[f"M{s}_{key}"] = None

            # Cross-round streak per slot
            hist = slot_history[s]
            if len(hist) >= 2:
                fv[f"M{s}_streak2"] = ''.join(hist[-2:])
            if len(hist) >= 3:
                fv[f"M{s}_streak3"] = ''.join(hist[-3:])
            slot_history[s].append(feats.get('outcome', '?'))
            if len(slot_history[s]) > 3:
                slot_history[s].pop(0)

            # Track running goal averages per slot
            t = m['hg'] + m['ag']
            slot_goal_history[s].append(t)
            if len(slot_goal_history[s]) > 5:
                slot_goal_history[s].pop(0)

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

        # Multi-slot sequence features — detect repeating patterns across slots
        matches_by_n = {m['n']: m for m in matches}
        def _seq(slots, key):
            return ''.join(str(matches_by_n.get(s, {}).get(key, '?')) for s in slots)
        fv['R_M1M2M3_outcome'] = _seq([1,2,3], 'outcome')
        fv['R_M1M2M3_parity']  = _seq([1,2,3], 'parity')
        fv['R_M5M6M7_outcome'] = _seq([5,6,7], 'outcome')
        fv['R_M5M6M7_parity']  = _seq([5,6,7], 'parity')
        fv['R_M8M9M10_outcome'] = _seq([8,9,10], 'outcome')
        fv['R_M8M9M10_parity']  = _seq([8,9,10], 'parity')

        # Team rotation features — which teams in which slots
        for m in matches:
            s = m['n']
            hs_tmp = standings_map.get(m['home_team'], {})
            as_tmp = standings_map.get(m['away_team'], {})
            h_str = hs_tmp.get('position', 10)
            a_str = as_tmp.get('position', 10)
            fv[f'M{s}_str_mismatch'] = 'blowout' if abs(h_str - a_str) >= 8 else ('close' if abs(h_str - a_str) <= 3 else 'mid')
            fv[f'M{s}_h_tier'] = 'top' if h_str <= 4 else ('mid' if h_str <= 10 else 'bottom')
            fv[f'M{s}_a_tier'] = 'top' if a_str <= 4 else ('mid' if a_str <= 10 else 'bottom')
            fv[f'M{s}_same_tier'] = fv[f'M{s}_h_tier'] == fv[f'M{s}_a_tier']

            # Team ID from event_id — stable identifier even as names rotate
            eid = m.get('event_id', 0)
            team_id = eid % 20 if eid else None
            fv[f'M{s}_team_id'] = str(team_id) if team_id is not None else None

        # Poisson goal expectation per team — rolling avg goals scored/conceded
        for m in matches:
            s, ht, at = m['n'], m['home_team'], m['away_team']
            team_goal_history[ht].append(m['hg'])
            team_goal_history[at].append(m['ag'])
            if len(team_goal_history[ht]) > 10: team_goal_history[ht].pop(0)
            if len(team_goal_history[at]) > 10: team_goal_history[at].pop(0)
            if len(team_goal_history[ht]) >= 3:
                fv[f'M{s}_h_exp_goals'] = str(min(int(round(sum(team_goal_history[ht][:-1]) / len(team_goal_history[ht][:-1]), 1)*2), 4))
            if len(team_goal_history[at]) >= 3:
                fv[f'M{s}_a_exp_goals'] = str(min(int(round(sum(team_goal_history[at][:-1]) / len(team_goal_history[at][:-1]), 1)*2), 4))

        # Overround bias — when market is inefficient, the longest shot has hidden value
        for m in matches:
            s = m['n']
            mkts = m.get('pre_markets', {})
            x2 = mkts.get('1X2', [])
            h_od = float(x2[0]['odd_value']) if len(x2) > 0 else None
            d_od = float(x2[1]['odd_value']) if len(x2) > 1 else None
            a_od = float(x2[2]['odd_value']) if len(x2) > 2 else None
            if h_od and d_od and a_od:
                implied_sum = 1/h_od + 1/d_od + 1/a_od
                overround = round(implied_sum - 1, 3)
                fv[f'M{s}_overround'] = 'tight' if overround < 0.05 else ('normal' if overround < 0.10 else 'wide')
                if overround > 0.08:
                    odds_list = sorted([(h_od, 'H'), (d_od, 'D'), (a_od, 'A')])
                    fv[f'M{s}_value_side'] = odds_list[-1][1]  # the underpriced longshot

        # Round ID decoded: the gap is almost always 11 or 22
        rid_raw = rd['round_id']
        try:
            rid_int = int(rid_raw)
            fv['R_cycle'] = str(rid_int // 11)
            fv['R_cycle_pos'] = str(rid_int % 11)
            fv['R_rid_parity'] = 'E' if rid_int % 2 == 0 else 'O'
        except:
            pass

        # Team rotation signature: which 10 teams are home in this round
        homes = tuple(sorted([m['home_team'] for m in matches]))
        aways = tuple(sorted([m['away_team'] for m in matches]))
        fv['R_home_sig'] = str(hash(homes) % 10000)
        fv['R_away_sig'] = str(hash(aways) % 10000)
        home_key = hash(homes) % 10000
        if home_key in _seen_home_sigs:
            fv['R_home_repeat'] = 'yes'
        else:
            fv['R_home_repeat'] = 'no'
            _seen_home_sigs.add(home_key)

        # Jump
        rid_raw = rd['round_id']
        try:
            rid_int = int(rid_raw)
            if prev_rid is not None:
                jump = rid_int - prev_rid
                fv['R_jump'] = 'normal' if jump <= 15 else ('skip' if jump <= 30 else 'break')
            else:
                fv['R_jump'] = 'normal'
            prev_rid = rid_int
        except (ValueError, TypeError):
            fv['R_jump'] = 'normal'
            prev_rid = None

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
    
    # Only mine on outcome/parity/odds_fav — that's what we bet on
    cond_keys_1feat = all_keys
    
    # Focus conditions: R-level features + slot 5,6,7,10 features
    cond_keys_focus = [k for k in all_keys if any(
        k.startswith(f"M{s}_") for s in [5,6,7,10]
    ) or k.startswith('R_')]
    
    # When n < 50, drastically reduce scope to finish in time
    if n < 50:
        # Only mine outcome + parity + ht features for the 4 key slots
        target_keys = [k for k in all_keys if any(
            k.endswith(sfx) for sfx in ['_outcome', '_parity', '_ht_parity', '_ht_outcome',
                                         '_ht_ft_trans', '_both_late', '_any_late', '_early_goal',
                                         '_running_goals']
        )]
        # Only use 1-feat conds for focus + all 1-feat for target slots 5,6,7,10
        cond_keys_1feat = [k for k in all_keys if any(
            k.startswith(f"M{s}_") for s in [5,6,7,10]
        ) or k.startswith('R_') or any(k.endswith(sfx2) for sfx2 in ['_streak','_trend','_h2h_last','_h2h_hw','_pos_diff','_pts_diff','_form_diff','_odds_fav','_prob','_overround','_consensus','_cs_','_tg_','_mg_','_gg_','_tg15_','_tg25_','_fts_','_h1x2_','_t1g_','_t2g_','_dc_'])]
        # Skip 3-feat entirely when n < 50
        _skip_3feat = True
    else:
        target_keys = [k for k in all_keys if any(
            k.endswith(sfx) for sfx in ['_outcome', '_parity', '_total', '_margin', '_cs', '_both_score',
                                         '_odds_fav', '_h_trend', '_a_trend',
                                         '_ht_parity', '_ht_outcome', '_ht_ft_trans',
                                         '_both_late', '_any_late', '_early_goal',
                                         '_running_goals']
        )]
        _skip_3feat = False
    
    if not target_keys:
        target_keys = all_keys[:20]

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

        for target_key in target_keys:
            target_vals = {j: fvecs[j].get(target_key) for _, j in pairs}
            all_tv = [v for v in target_vals.values() if v is not None]
            if len(all_tv) < 3:
                continue
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

            # 2-feature (skip when low n — too expensive)
            if n >= 50:
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
    top_focus = sorted(cond_keys_focus, key=lambda k: -key_scores.get(k, 0))[:10]

    # 3-feature (only when n >= 50 and top-10 focus keys)
    if not _skip_3feat and n >= 50:
        pairs = [(i, i+lag) for i in range(n-lag)]
        for target_key in target_keys:
            if 'h_prob' in target_key or 'a_prob' in target_key or 'd_prob' in target_key:
                continue
            target_vals = {j: fvecs[j].get(target_key) for _, j in pairs}
            all_tv = [v for v in target_vals.values() if v is not None]
            if not all_tv or len(all_tv) < 6: continue
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

        # Sequence rules: 3-round exact sequences per slot
        if lag == 1 and n >= 10:
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

        # Multi-slot sequence rules: R_M5M6M7_parity="EEE" in N → target in N+1
        if n >= 6:
            for seq_key in ['R_M5M6M7_parity', 'R_M5M6M7_outcome', 'R_M8M9M10_parity', 'R_M8M9M10_outcome', 'R_M1M2M3_parity', 'R_M1M2M3_outcome']:
                for _target_key in target_keys:
                    seq_values = [(i, fvecs[i].get(seq_key), fvecs[i+lag].get(_target_key)) for i in range(n-lag)]
                    seq_values = [(i, sv, tv) for i, sv, tv in seq_values if sv is not None and tv is not None]
                    if len(seq_values) < min_hits: continue
                    vc = Counter()
                    for i, sv, tv in seq_values:
                        vc[(sv, tv)] += 1
                    for (sv, tv), hits in vc.items():
                        total = sum(v for (s,_), v in vc.items() if s == sv)
                        if total < min_hits: continue
                        prec = hits / total
                        if prec >= min_precision:
                            base_rate = base_rates.get(_target_key, {}).get(tv, 0.5)
                            rules.append({
                                'target': f"{_target_key}={tv}",
                                'conditions': {seq_key: sv},
                                'lag': lag,
                                'hits': hits, 'total': total,
                                'precision': round(prec, 3),
                                'recall': 0.0,
                                'lift': round(prec/base_rate, 2) if base_rate else 1.0,
                                'pvalue': 1.0, 'stable': True,
                                'tentative': total < 8,
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
            
            # Batch insert in chunks of 500
            rows = []
            for r in new_rules:
                ev = compute_ev(r)
                key = (r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag'])
                prev = prev_rules.get(key, {})
                hist = prev.get('prec_history', []) if isinstance(prev.get('prec_history'), list) else []
                hist = (hist + [r['precision']])[-10:]
                rows.append((
                    r['target'], json.dumps(r['conditions']), r['lag'],
                    r['hits'], r['total'], r['precision'], r['recall'], ev,
                    r.get('lift', 1.0), r.get('pvalue', 1.0),
                    r.get('stable', True), r.get('ensemble', 0),
                    r.get('chained_via'), json.dumps(hist),
                    datetime.now(), rounds_used,
                    'tentative' if r.get('tentative') else 'active'
                ))
            chunk_size = 500
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i:i+chunk_size]
                placeholders = ','.join(['(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)' for _ in chunk])
                vals = []
                for row in chunk:
                    vals.extend(row)
                cur.execute(
                    'INSERT INTO rules (target, conditions, lag, hits, total, precision, recall, ev, '
                    'lift, pvalue, stable, ensemble, chained_via, prec_history, '
                    'discovered_at, rounds_used, status) VALUES ' + placeholders,
                    vals
                )

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
