#!/usr/bin/env python3
"""
Global Pattern Learner
Reads from unified rounds table across ALL sources/collectors.
Normalizes data to a common format before mining.
New collectors just need to write to the rounds table via db.save_round() —
no changes needed here.
"""
import os, json, time, itertools, math
from collections import Counter, defaultdict
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get('DATABASE_URL', '')
MIN_ROUNDS   = 15
RUN_EVERY    = 300


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global_rules (
                    id SERIAL PRIMARY KEY,
                    target TEXT,
                    conditions JSONB,
                    lag INT,
                    hits INT,
                    total INT,
                    precision FLOAT,
                    recall FLOAT,
                    ev FLOAT,
                    source TEXT DEFAULT 'all',
                    sources TEXT[],
                    discovered_at TIMESTAMP,
                    rounds_used INT,
                    status TEXT DEFAULT 'active'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global_failed_rules (
                    id SERIAL PRIMARY KEY,
                    target TEXT,
                    conditions JSONB,
                    lag INT,
                    initial_precision FLOAT,
                    final_precision FLOAT,
                    hits INT,
                    rounds_used INT,
                    failed_at TIMESTAMP
                )
            """)
        conn.commit()


def load_rounds(source=None, limit=500):
    """Load rounds — fetch data column directly, limit rows."""
    with get_db() as conn:
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT data, source FROM rounds WHERE source=%s ORDER BY round_id DESC LIMIT %s",
                    (source, limit))
            else:
                cur.execute(
                    "SELECT data, source FROM rounds ORDER BY round_id DESC LIMIT %s",
                    (limit,))
            rows = cur.fetchall()
            # Return oldest-first
            return [(r['data'], r['source']) for r in reversed(rows)]


def load_previous_rules():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT target, conditions, lag, precision, hits FROM global_rules WHERE status='active'")
                return {(r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag']): r
                        for r in cur.fetchall()}
    except:
        return {}


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_match(m, source):
    """
    Convert any collector's match format to a common dict.
    Handles field name differences across sources.
    """
    # Goals
    hg = m.get('hg') or m.get('home_goals') or 0
    ag = m.get('ag') or m.get('away_goals') or 0
    try: hg, ag = int(hg), int(ag)
    except: hg, ag = 0, 0

    # Teams
    home = m.get('home') or m.get('home_team') or ''
    away = m.get('away') or m.get('away_team') or ''

    # Odds — normalize to {H, D, A}
    odds_raw = m.get('odds') or m.get('pre_markets') or {}
    x2 = (odds_raw.get('1x2') or odds_raw.get('1X2') or
          odds_raw.get('1X2', []))
    h_odd = d_odd = a_odd = None
    if isinstance(x2, dict):
        h_odd = x2.get('1') or x2.get('H')
        d_odd = x2.get('X') or x2.get('D')
        a_odd = x2.get('2') or x2.get('A')
    elif isinstance(x2, list) and len(x2) >= 3:
        h_odd = x2[0].get('odd_value')
        d_odd = x2[1].get('odd_value')
        a_odd = x2[2].get('odd_value')
    try: h_odd, d_odd, a_odd = float(h_odd), float(d_odd), float(a_odd)
    except: h_odd = d_odd = a_odd = None

    t = hg + ag
    
    # HT score
    ht_str = m.get('ht')
    ht_parity, ht_outcome = None, None
    if ht_str and ':' in str(ht_str):
        try:
            parts = str(ht_str).split(':')
            ht_hg, ht_ag = int(parts[0]), int(parts[1])
            ht_t = ht_hg + ht_ag
            ht_parity = None if ht_t == 0 else ('E' if ht_t % 2 == 0 else 'O')
            ht_outcome = 'W' if ht_hg > ht_ag else ('L' if ht_hg < ht_ag else 'D')
        except: pass
    
    # Goal timing
    home_score_times = m.get('home_score_times') or m.get('homeGoals') or []
    away_score_times = m.get('away_score_times') or m.get('awayGoals') or []
    h_late = sum(1 for t in home_score_times if isinstance(t,(int,float)) and t > 60) if isinstance(home_score_times, list) else 0
    a_late = sum(1 for t in away_score_times if isinstance(t,(int,float)) and t > 60) if isinstance(away_score_times, list) else 0
    
    # Extract GG odds (GG, Both Teams To Score)
    gg_yes = None
    gg_raw = odds_raw.get('GG', [])
    if isinstance(gg_raw, list):
        for o in gg_raw:
            if isinstance(o, dict) and o.get('outcome_id') in ('Y','Yes'):
                try: gg_yes = float(o['odd_value'])
                except: pass

    # Extract O/U 2.5
    tg25_o = None
    for market_key in ('TG25', 'OV/UN 2.5', 'Total Score Over/Under - FT'):
        tg_raw = odds_raw.get(market_key, [])
        if isinstance(tg_raw, list):
            for o in tg_raw:
                if isinstance(o, dict):
                    name = o.get('outcome_name','').lower()
                    oid = o.get('outcome_id','').lower()
                    if 'over' in name or oid == 'o':
                        try: tg25_o = float(o['odd_value'])
                        except: pass
    
    return {
        'n':             m.get('n', 0),
        'home':          home,
        'away':          away,
        'hg':            hg,
        'ag':            ag,
        'total':         t,
        'parity':        None if t == 0 else ('E' if t % 2 == 0 else 'O'),
        'outcome':       'W' if hg > ag else ('L' if hg < ag else 'D'),
        'cs':            hg == 0 or ag == 0,
        'both_score':    hg > 0 and ag > 0,
        'margin':        abs(hg - ag),
        'h_odd':         h_odd,
        'd_odd':         d_odd,
        'a_odd':         a_odd,
        'gg_yes':        gg_yes,
        'tg25_o':        tg25_o,
        'ht_parity':     ht_parity,
        'ht_outcome':    ht_outcome,
        'h_late_goals':  h_late,
        'a_late_goals':  a_late,
        'any_late':      h_late > 0 or a_late > 0,
        'source':        source,
    }


def normalize_standings(standings):
    """Normalize standings from any source to {team_name: {position, points, team_form}}."""
    result = {}
    for s in (standings or []):
        name = s.get('team_name') or s.get('team') or ''
        result[name] = {
            'position':  s.get('position', 10),
            'points':    s.get('points', 0),
            'team_form': s.get('team_form', ''),
        }
    return result


# ── Feature extraction ────────────────────────────────────────────────────────

def form_score(f): return f.count('W')*3 + f.count('D') if f else 0
def form_trend(f):
    if len(f) < 6: return 0
    return form_score(f[:3]) - form_score(f[3:])

def prob_bucket(odd):
    if not odd: return None
    p = 1/odd*100
    if p >= 60: return '60+'
    if p >= 50: return '50-60'
    if p >= 40: return '40-50'
    if p >= 30: return '30-40'
    return '<30'

def discretize(val, key):
    if val is None: return None
    if key in ('h_odd','d_odd','a_odd','gg_yes','tg25_o'):
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
    return val


def build_fvecs(rounds_with_source):
    """Build feature vectors from normalized rounds."""
    fvecs = []
    sources_used = []
    slot_history = defaultdict(list)

    for rd, source in rounds_with_source:
        # Handle case where rd comes back as tuple or non-dict from DB
        if isinstance(rd, (list, tuple)):
            rd = rd[0] if rd else {}
        if not isinstance(rd, dict):
            continue
        standings_map = normalize_standings(rd.get('standings', []))
        fv = {}

        for m_raw in rd.get('matches', []):
            m = normalize_match(m_raw, source)
            s = m['n']
            hs  = standings_map.get(m['home'], {})
            as_ = standings_map.get(m['away'], {})
            h_form = hs.get('team_form', '')
            a_form = as_.get('team_form', '')

            feats = {
                'outcome':   m['outcome'],
                'parity':    m['parity'],
                'cs':        m['cs'],
                'both_score':m['both_score'],
                'total':     m['total'],
                'margin':    m['margin'],
                'h_odd':     m['h_odd'],
                'd_odd':     m['d_odd'],
                'a_odd':     m['a_odd'],
                'h_prob':    prob_bucket(m['h_odd']),
                'a_prob':    prob_bucket(m['a_odd']),
                'pos_diff':  as_.get('position',10) - hs.get('position',10),
                'pts_diff':  hs.get('points',0) - as_.get('points',0),
                'form_diff': form_score(h_form) - form_score(a_form),
                'h_trend':   'up' if form_trend(h_form)>0 else ('down' if form_trend(h_form)<0 else 'flat'),
                'a_trend':   'up' if form_trend(a_form)>0 else ('down' if form_trend(a_form)<0 else 'flat'),
                'gg_yes':    m.get('gg_yes'),
                'tg25_o':    m.get('tg25_o'),
                'ht_parity': m.get('ht_parity'),
                'ht_outcome':m.get('ht_outcome'),
                'both_late': m.get('any_late') and m.get('h_late_goals',0) > 0 and m.get('a_late_goals',0) > 0,
                'any_late':  m.get('any_late', False),
            }
            if m['h_odd'] and m['d_odd'] and m['a_odd']:
                if m['h_odd'] < m['a_odd'] and m['h_odd'] < m['d_odd']: feats['odds_fav'] = 'H'
                elif m['a_odd'] < m['h_odd'] and m['a_odd'] < m['d_odd']: feats['odds_fav'] = 'A'
                else: feats['odds_fav'] = 'D'

            for key, val in feats.items():
                fv[f"M{s}_{key}"] = discretize(val, key)

            # Cross-round streaks
            hist = slot_history[s]
            if len(hist) >= 2: fv[f"M{s}_streak2"] = ''.join(hist[-2:])
            if len(hist) >= 3: fv[f"M{s}_streak3"] = ''.join(hist[-3:])
            slot_history[s].append(m['outcome'])
            if len(slot_history[s]) > 3: slot_history[s].pop(0)

        # Round-level features
        matches = [normalize_match(m, source) for m in rd.get('matches', [])]
        totals = [m['total'] for m in matches]
        fv['R_total_parity'] = 'E' if sum(totals) % 2 == 0 else 'O'
        fv['R_draws']        = str(sum(1 for m in matches if m['outcome'] == 'D'))
        fv['R_cs']           = str(sum(1 for m in matches if m['cs']))
        fv['R_home_wins']    = str(sum(1 for m in matches if m['outcome'] == 'W'))
        fv['R_source']       = source  # source as a feature — cross-source rules possible

        fvecs.append(fv)
        sources_used.append(source)

    return fvecs, sources_used


# ── Rule mining ───────────────────────────────────────────────────────────────

def mine_rules(fvecs, sources_used, min_hits=3, min_precision=0.78):
    """
    Mine predictive rules from feature vectors.
    
    Enhancements:
    - Bayesian precision: (hits + prior_mass) / (total + prior_strength)
      to avoid overvaluing tiny 100% samples.
    - Dynamic min_precision: lower threshold for larger sample sizes.
    - Sorting by Bayesian EV (= bayes_precision * hits * log10(total+1))
      so high-volume rules at moderate precision can rank above tiny 100% ones.
    - 100% rules are still kept regardless of sample size (user requirement).
    """
    n = len(fvecs)
    if n == 0:
        return []
    all_keys = list(fvecs[0].keys())
    focus_keys = [k for k in all_keys if any(
        k.startswith(f"M{s}_") for s in [5,6,7,10]
    ) or k.startswith('R_')][:30]

    # Only predict outcome/parity/cs targets
    target_keys = [k for k in all_keys if k.endswith('_outcome') or
                   k.endswith('_parity') or k.endswith('_cs')]

    # Bayesian prior: assume 36% baseline (3-way average) with 10 pseudo-observations
    PRIOR_PRECISION = 0.36
    PRIOR_STRENGTH  = 10
    PRIOR_MASS      = PRIOR_PRECISION * PRIOR_STRENGTH

    rules = []

    for lag in [1, 2, 3]:
        pairs = [(i, i+lag) for i in range(n-lag)]

        for target_key in target_keys:
            target_vals = {j: fvecs[j].get(target_key) for _, j in pairs}

            # 1-feature
            for ck in all_keys:
                vc = defaultdict(Counter)
                for i, j in pairs:
                    cv, tv = fvecs[i].get(ck), target_vals.get(j)
                    if cv is not None and tv is not None:
                        w = 2 if i >= n * 0.6 else 1
                        vc[cv][tv] += w
                for cv, tc in vc.items():
                    total = sum(tc.values())
                    if total < min_hits:
                        continue
                    for tv, hits in tc.items():
                        raw_prec = hits / total
                        bayes_prec = (hits + PRIOR_MASS) / (total + PRIOR_STRENGTH)
                        recall = hits / max(1, sum(tc2.get(tv,0) for tc2 in vc.values()))
                        src_set = list({sources_used[i] for i, j in pairs
                                       if fvecs[i].get(ck)==cv and target_vals.get(j)==tv})

                        # Dynamic threshold: larger samples can use lower precision
                        if total >= 100:
                            effective_threshold = 0.60
                        elif total >= 50:
                            effective_threshold = 0.65
                        elif total >= 30:
                            effective_threshold = 0.70
                        elif total >= 15:
                            effective_threshold = 0.75
                        else:
                            effective_threshold = min_precision

                        # Keep rule if: (bayesian >= threshold) OR (raw 100% perfect)
                        if bayes_prec >= effective_threshold or raw_prec == 1.0:
                            bayes_ev = round(bayes_prec * hits * (1 + math.log10(total + 1)), 2)
                            rules.append({'target': f"{target_key}={tv}",
                                          'conditions': {ck: cv}, 'lag': lag,
                                          'hits': hits, 'total': total,
                                          'precision': round(raw_prec, 3),
                                          'bayes_precision': round(bayes_prec, 3),
                                          'bayes_ev': bayes_ev,
                                          'recall': round(recall, 3),
                                          'sources': src_set,
                                          'tentative': total < 8})

            # 2-feature
            for k1, k2 in itertools.combinations(focus_keys, 2):
                vc = defaultdict(Counter)
                for i, j in pairs:
                    v1,v2,tv = fvecs[i].get(k1),fvecs[i].get(k2),target_vals.get(j)
                    if None not in (v1,v2,tv):
                        w = 2 if i >= n*0.6 else 1
                        vc[(v1,v2)][tv] += w
                for (v1,v2), tc in vc.items():
                    total = sum(tc.values())
                    if total < min_hits:
                        continue
                    for tv, hits in tc.items():
                        raw_prec = hits / total
                        bayes_prec = (hits + PRIOR_MASS) / (total + PRIOR_STRENGTH)
                        recall = hits / max(1, sum(tc2.get(tv,0) for tc2 in vc.values()))
                        src_set = list({sources_used[i] for i,j in pairs
                                       if fvecs[i].get(k1)==v1 and fvecs[i].get(k2)==v2
                                       and target_vals.get(j)==tv})

                        if total >= 100:
                            effective_threshold = 0.60
                        elif total >= 50:
                            effective_threshold = 0.65
                        elif total >= 30:
                            effective_threshold = 0.70
                        elif total >= 15:
                            effective_threshold = 0.75
                        else:
                            effective_threshold = min_precision

                        if bayes_prec >= effective_threshold or raw_prec == 1.0:
                            bayes_ev = round(bayes_prec * hits * (1 + math.log10(total + 1)), 2)
                            rules.append({'target': f"{target_key}={tv}",
                                          'conditions': {k1:v1,k2:v2}, 'lag': lag,
                                          'hits': hits, 'total': total,
                                          'precision': round(raw_prec, 3),
                                          'bayes_precision': round(bayes_prec, 3),
                                          'bayes_ev': bayes_ev,
                                          'recall': round(recall, 3),
                                          'sources': src_set,
                                          'tentative': total < 8})

    # Deduplicate — keep the one with highest bayes_ev
    seen = {}
    for r in rules:
        key = (r['target'], json.dumps(r['conditions'], sort_keys=True), r['lag'])
        if key not in seen or r.get('bayes_ev', 0) > seen[key].get('bayes_ev', 0):
            seen[key] = r

    # Conflict detection
    cond_to_tgts = defaultdict(list)
    for r in seen.values():
        ck = (json.dumps(r['conditions'], sort_keys=True), r['lag'])
        cond_to_tgts[ck].append(r['target'])
    conflicts = {ck for ck, tgts in cond_to_tgts.items() if len(set(tgts)) > 1}
    for r in seen.values():
        ck = (json.dumps(r['conditions'], sort_keys=True), r['lag'])
        r['conflict'] = ck in conflicts

    # Sort by bayes_ev descending (best EV first)
    return sorted(seen.values(), key=lambda r: -r.get('bayes_ev', 0))


def save_rules(rules, prev_rules, rounds_used, source='all'):
    with get_db() as conn:
        with conn.cursor() as cur:
            new_keys = {(r['target'], json.dumps(r['conditions'],sort_keys=True), r['lag'], source)
                        for r in rules}
            for key, prev in prev_rules.items():
                if key not in new_keys and prev['precision'] >= 0.85:
                    cur.execute("""
                        INSERT INTO global_failed_rules
                        (target, conditions, lag, initial_precision, final_precision, hits, rounds_used, failed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (prev['target'], json.dumps(prev['conditions']), prev['lag'],
                          prev['precision'], 0.0, prev['hits'], rounds_used, datetime.now()))

            cur.execute("DELETE FROM global_rules WHERE source=%s", (source,))
            for r in rules:
                ev = r.get('bayes_ev', round(r['precision'] * r['hits'], 2))
                cur.execute("""
                    INSERT INTO global_rules
                    (target, conditions, lag, hits, total, precision, recall, ev,
                     source, sources, discovered_at, rounds_used, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (r['target'], json.dumps(r['conditions']), r['lag'],
                      r['hits'], r['total'], r['precision'], r['recall'], ev,
                      source, r.get('sources', []), datetime.now(), rounds_used,
                      'tentative' if r.get('tentative') else 'active'))
        conn.commit()


if __name__ == '__main__':
    init_tables()
    print("Global Learner started")

    while True:
        try:
            all_rounds = load_rounds()
            ts = datetime.now().strftime('%H:%M:%S')
            source_counts = Counter(s for _, s in all_rounds)
            print(f"\n[{ts}] {len(all_rounds)} rounds — {dict(source_counts)}")

            if len(all_rounds) < MIN_ROUNDS:
                print(f"  Need {MIN_ROUNDS} minimum...")
            else:
                prev_rules = load_previous_rules()

                # Mine per source
                for src in source_counts:
                    src_rounds = [(rd, s) for rd, s in all_rounds if s == src]
                    if len(src_rounds) < MIN_ROUNDS:
                        continue
                    fvecs, sources_used = build_fvecs(src_rounds)
                    rules = mine_rules(fvecs, sources_used)
                    save_rules(rules, {}, len(src_rounds), source=src)
                    print(f"  [{src}] {len(rules)} rules from {len(src_rounds)} rounds")

                # Mine globally (all sources combined)
                fvecs, sources_used = build_fvecs(all_rounds)
                rules = mine_rules(fvecs, sources_used)
                save_rules(rules, prev_rules, len(all_rounds), source='all')

                active = [r for r in rules if not r.get('conflict')]
                print(f"  [all] {len(active)} rules from {len(all_rounds)} rounds")

                # Show top rules grouped by slot
                by_slot = defaultdict(list)
                for r in active[:100]:
                    slot = r['target'].split('_')[0]
                    by_slot[slot].append(r)
                for slot in sorted(by_slot.keys()):
                    print(f"\n  [{slot}]")
                    for r in by_slot[slot][:3]:
                        src_str = ','.join(r.get('sources',[])) or 'all'
                        flags = ('⚠' if r.get('tentative') else '') + ('⚡' if r.get('conflict') else '')
                        print(f"    lag={r['lag']} {r['precision']:.0%} ({r['hits']}/{r['total']}) "
                              f"src={src_str} {flags} | {r['target']} | IF {r['conditions']}")

        except Exception as e:
            import traceback; traceback.print_exc()

        time.sleep(RUN_EVERY)
