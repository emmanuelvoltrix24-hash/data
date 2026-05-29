#!/usr/bin/env python3
"""
VFL Prediction Engine v2
Live poller that catches round N results → queries DB rules → predicts round N+1 (all 10 matches).
Stores predictions for backtesting. No Playwright dependency.
"""
import os, sys, json, time, requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# Ensure local module path
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# ── Config ─────────────────────────────────────────────────────────────────
PERIODS_URL = 'https://vl.betkraft.co.uk/periods/1'
LIVE_URL    = 'https://vl.betkraft.co.uk/live'
DATA_URL    = 'https://vl.betkraft.co.uk/data'
STANDING_URL = 'https://vl.betkraft.co.uk/standing/1/0'

DB_URL = os.environ.get('DATABASE_URL', '')
POLL_INTERVAL = 3  # seconds between polls
BET_WINDOW = 180   # seconds before next round closes

# ── API calls (no auth needed) ──────────────────────────────────────────────
def get_periods():
    r = requests.get(PERIODS_URL, timeout=10)
    return r.json()['data']['periods']

def get_live(period: dict) -> Optional[list]:
    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
    r = requests.post(LIVE_URL, json=payload, timeout=10)
    if r.status_code == 200:
        data = r.json()
        if data.get('status_code') == 200 and data.get('data'):
            return data['data'].get('live', [])
    return None

def get_standings() -> dict:
    r = requests.get(STANDING_URL, timeout=10)
    if r.status_code == 200:
        return {s['team_name']: s for s in r.json()['data']['standings']}
    return {}

def get_next_odds() -> list:
    r = requests.get(DATA_URL, timeout=10)
    if r.status_code == 200:
        try:
            return r.json()['data']['matches']
        except:
            pass
    return []

def to_utc(s: str) -> datetime:
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

# ── DB Rules Loader ──────────────────────────────────────────────────────────
_rules_cache = {'rules': None, 'loaded_at': 0}

def load_rules(min_prec: float = 0.75, min_total: int = 50, max_rules: int = 5000):
    """Load high-confidence rules from global_rules, cached for 5 min."""
    import time as t
    now = t.time()
    if now - _rules_cache['loaded_at'] < 300 and _rules_cache['rules'] is not None:
        return _rules_cache['rules']
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT target, conditions::text, lag, precision, hits, total, source
                    FROM global_rules
                    WHERE status='active' AND precision >= %s AND total >= %s
                    ORDER BY (precision * hits * LOG(total+1)) DESC
                    LIMIT %s
                """, (min_prec, min_total, max_rules))
                rules = cur.fetchall()
        for r in rules:
            r['conditions'] = json.loads(r['conditions'])
        _rules_cache['rules'] = rules
        _rules_cache['loaded_at'] = t.time()
        return rules
    except Exception as e:
        return []

# ── Feature extraction ──────────────────────────────────────────────────────
def parity(total: int) -> Optional[str]:
    return None if total == 0 else ('E' if total % 2 == 0 else 'O')

def bucket_odds(odd: float) -> str:
    if odd >= 6.0: return 'vhigh'
    if odd >= 3.5: return 'high'
    if odd >= 2.2: return 'med'
    if odd >= 1.6: return 'low'
    return 'vlow'

def bucket_prob(prob: float) -> str:
    if prob >= 60: return '60+'
    if prob >= 50: return '50-60'
    if prob >= 40: return '40-50'
    if prob >= 30: return '30-40'
    return '<30'

def extract_features(matches: list, standings: dict) -> dict:
    """Extract all features from a completed round's matches.
    Matches can have 'n' field or be positional (0-indexed list).
    """
    feat = {}
    for i, m in enumerate(matches):
        n = m.get('n', i + 1)  # use n field or position
        hg = int(m.get('hg', 0))
        ag = int(m.get('ag', 0))
        total = hg + ag
        ht = m.get('home_team', '')
        at = m.get('away_team', '')
        
        # Basic
        feat[f'M{n}_hg'] = hg
        feat[f'M{n}_ag'] = ag
        feat[f'M{n}_total'] = total
        feat[f'M{n}_parity'] = parity(total)
        feat[f'M{n}_outcome'] = 'W' if hg > ag else ('L' if hg < ag else 'D')
        feat[f'M{n}_cs'] = (hg == 0 or ag == 0)
        feat[f'M{n}_both_score'] = (hg > 0 and ag > 0)
        feat[f'M{n}_result'] = f"{hg}:{ag}"
        
        # Half-time (if available)
        ht_hg = m.get('ht_hg')
        ht_ag = m.get('ht_ag')
        if ht_hg is not None and ht_ag is not None:
            ht_total = int(ht_hg) + int(ht_ag)
            feat[f'M{n}_ht_parity'] = parity(ht_total)
            feat[f'M{n}_ht_outcome'] = 'W' if int(ht_hg) > int(ht_ag) else ('L' if int(ht_hg) < int(ht_ag) else 'D')
            feat[f'M{n}_ht_hg'] = int(ht_hg)
            feat[f'M{n}_ht_ag'] = int(ht_ag)
        
        # Odds-based features (from match's markets)
        markets = m.get('markets', [])
        if markets:
            outcomes = markets[0].get('outcomes', [])
            if len(outcomes) >= 3:
                h_odd = float(outcomes[0].get('odd_value', 1))
                d_odd = float(outcomes[1].get('odd_value', 1))
                a_odd = float(outcomes[2].get('odd_value', 1))
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
                feat[f'M{n}_h_prob_bucket'] = bucket_prob(h_prob)
                feat[f'M{n}_a_prob_bucket'] = bucket_prob(a_prob)
                feat[f'M{n}_odds_fav'] = 'H' if h_odd < a_odd else 'A'
        
        # Standings-based features
        hs = standings.get(ht, {})
        as_ = standings.get(at, {})
        h_pos = hs.get('position')
        a_pos = as_.get('position')
        h_pts = hs.get('points', 0)
        a_pts = as_.get('points', 0)
        h_form = hs.get('team_form', '')
        a_form = as_.get('team_form', '')
        
        if h_pos is not None and a_pos is not None:
            pos_diff = a_pos - h_pos
            feat[f'M{n}_pos_diff'] = pos_diff
            if pos_diff >= 8: feat[f'M{n}_pos_diff_bucket'] = 'H++'
            elif pos_diff >= 4: feat[f'M{n}_pos_diff_bucket'] = 'H+'
            elif pos_diff >= -3: feat[f'M{n}_pos_diff_bucket'] = 'even'
            elif pos_diff >= -7: feat[f'M{n}_pos_diff_bucket'] = 'A+'
            else: feat[f'M{n}_pos_diff_bucket'] = 'A++'
        
        pts_diff = h_pts - a_pts
        feat[f'M{n}_pts_diff'] = pts_diff
        if pts_diff >= 15: feat[f'M{n}_pts_diff_bucket'] = 'H++'
        elif pts_diff >= 8: feat[f'M{n}_pts_diff_bucket'] = 'H+'
        elif pts_diff >= -7: feat[f'M{n}_pts_diff_bucket'] = 'even'
        elif pts_diff >= -14: feat[f'M{n}_pts_diff_bucket'] = 'A+'
        else: feat[f'M{n}_pts_diff_bucket'] = 'A++'
        
        # Form
        h_fs = h_form.count('W')*3 + h_form.count('D')
        a_fs = a_form.count('W')*3 + a_form.count('D')
        form_diff = h_fs - a_fs
        feat[f'M{n}_form'] = h_form[:6]
        feat[f'M{n}_a_form'] = a_form[:6]
        if form_diff >= 5: feat[f'M{n}_form_diff'] = 'H++'
        elif form_diff >= 2: feat[f'M{n}_form_diff'] = 'H+'
        elif form_diff >= -1: feat[f'M{n}_form_diff'] = 'even'
        elif form_diff >= -4: feat[f'M{n}_form_diff'] = 'A+'
        else: feat[f'M{n}_form_diff'] = 'A++'
        
        # Form trends
        if len(h_form) >= 6:
            h_recent = h_form[:3].count('W')*3 + h_form[:3].count('D')
            h_older = h_form[3:6].count('W')*3 + h_form[3:6].count('D')
            feat[f'M{n}_h_trend'] = 'up' if h_recent > h_older else ('down' if h_recent < h_older else 'flat')
        if len(a_form) >= 6:
            a_recent = a_form[:3].count('W')*3 + a_form[:3].count('D')
            a_older = a_form[3:6].count('W')*3 + a_form[3:6].count('D')
            feat[f'M{n}_a_trend'] = 'up' if a_recent > a_older else ('down' if a_recent < a_older else 'flat')
    
    # Round-level features
    outcomes = [feat.get(f'M{n}_outcome') for n in range(1, 11)]
    parities = [feat.get(f'M{n}_parity') for n in range(1, 11)]
    feat['R_home_wins'] = outcomes.count('W')
    feat['R_draws'] = outcomes.count('D')
    feat['R_away_wins'] = outcomes.count('L')
    feat['R_total_parity'] = 'O' if parities.count('O') > parities.count('E') else 'E'
    feat['R_cs'] = sum(1 for n in range(1, 11) if feat.get(f'M{n}_cs'))
    feat['R_both_score'] = sum(1 for n in range(1, 11) if feat.get(f'M{n}_both_score'))
    feat['R_source'] = 'betkraft'
    
    return feat

# ── Rule matching ───────────────────────────────────────────────────────────
def match_rule(rule: dict, features: dict) -> bool:
    """Check if a rule's conditions match the extracted features."""
    conds = rule['conditions']
    for k, v in conds.items():
        fv = features.get(k)
        if fv is None:
            return False
        # Convert to string for comparison
        if str(fv) != str(v):
            return False
    return True

def predict_round(prev_features: dict, next_matches: list, standings: dict, rules: list) -> list:
    """
    Predict all 10 matches of next round using DB rules.
    Returns list of predictions.
    """
    predictions = []
    
    # Group rules by target slot (M1-M10)
    slot_rules = {}
    for r in rules:
        target = r['target']
        for s in [f'M{n}_' for n in range(1, 11)]:
            if target.startswith(s):
                slot = s.rstrip('_')
                if slot not in slot_rules:
                    slot_rules[slot] = []
                slot_rules[slot].append(r)
                break
    
    for i, m in enumerate(next_matches, 1):
        slot = f'M{i}'
        pred = {
            'slot': slot,
            'home': m.get('home_team', '?'),
            'away': m.get('away_team', '?'),
            'event_id': m.get('event_id'),
            'predictions': [],
            'best_outcome': None,
            'best_parity': None,
            'confidence': 'LOW',
            'signal': '',
        }
        
        # Get odds for this match
        markets = m.get('markets', [])
        h_odd = d_odd = a_odd = None
        if markets and len(markets[0].get('outcomes', [])) >= 3:
            outcomes = markets[0]['outcomes']
            h_odd = float(outcomes[0].get('odd_value', 1))
            d_odd = float(outcomes[1].get('odd_value', 1))
            a_odd = float(outcomes[2].get('odd_value', 1))
        
        # Match rules for this slot
        matched = []
        for r in slot_rules.get(slot, []):
            if r['lag'] == 1 and match_rule(r, prev_features):
                matched.append(r)
        
        # Also check lag=2 and lag=3 rules if no lag=1 match
        if not matched:
            for r in slot_rules.get(slot, []):
                if r['lag'] in (2, 3) and match_rule(r, prev_features):
                    matched.append(r)
        
        if matched:
            # Sort by EV descending
            matched.sort(key=lambda x: x['precision'] * x['hits'] * (x['total'] ** 0.5), reverse=True)
            best = matched[0]
            target = best['target']
            prec = best['precision']
            
            # Extract outcome/parity from target
            if '_outcome=' in target or '_parity=' in target:
                parts = target.split('=')
                pred_type = parts[0].split('_')[-1]  # 'outcome' or 'parity'
                pred_val = parts[1]
                
                if pred_type == 'outcome':
                    pred['best_outcome'] = pred_val
                    pred['confidence'] = 'HIGH' if prec >= 0.80 else ('MEDIUM' if prec >= 0.75 else 'LOW')
                    pred['signal'] = f"★{pred_val}({prec:.0%}/{best['hits']}/{best['total']})"
                elif pred_type == 'parity':
                    pred['best_parity'] = pred_val
                    pred['confidence'] = 'HIGH' if prec >= 0.80 else ('MEDIUM' if prec >= 0.75 else 'LOW')
                    pred['signal'] = f"★PAR={pred_val}({prec:.0%}/{best['hits']}/{best['total']})"
                
                pred['predictions'].append({
                    'rule': f"{json.dumps(best['conditions'])} → {target}",
                    'precision': prec,
                    'hits': best['hits'],
                    'total': best['total'],
                    'lag': best['lag'],
                    'source': best.get('source', 'all'),
                })
        
        # Add odds-based default if no rule matched
        if not pred['best_outcome'] and not pred['best_parity']:
            if h_odd and a_odd:
                fav = 'H' if h_odd < a_odd else 'A'
                pred['best_outcome'] = fav
                pred['confidence'] = 'LOW'
                pred['signal'] = f"ODDS:{fav}(@min{h_odd if fav=='H' else a_odd:.2f})"
        
        predictions.append(pred)
    
    return predictions

# ── Display ─────────────────────────────────────────────────────────────────
def display_round(rid: int, prev_features: dict, next_matches: list, predictions: list):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n{'='*90}")
    print(f" [{ts}] ROUND #{rid} RESULTS + NEXT ROUND PREDICTIONS")
    print(f"{'='*90}")
    
    # Show results of previous round
    print(f"\n  ── ROUND #{rid} RESULTS ──")
    for n in range(1, 11):
        res = prev_features.get(f'M{n}_result', '?-?')
        ht = prev_features.get(f'M{n}_home_team', '?')
        at = prev_features.get(f'M{n}_away_team', '?')
        par = prev_features.get(f'M{n}_parity', '-')
        outcome = prev_features.get(f'M{n}_outcome', '-')
        cs = '✓' if prev_features.get(f'M{n}_cs') else '✗'
        print(f"    M{n}: {ht:<20} vs {at:<20} {res:>5}  par={par}  outcome={outcome}  cs={cs}")
    
    # Round-level stats
    print(f"  ── ROUND FEATURES ──")
    print(f"    Home wins: {prev_features.get('R_home_wins')}  Draws: {prev_features.get('R_draws')}  "
          f"CS: {prev_features.get('R_cs')}  Both: {prev_features.get('R_both_score')}  "
          f"Total par: {prev_features.get('R_total_parity')}")
    
    # Show predictions for next round
    print(f"\n  ── NEXT ROUND PREDICTIONS ──")
    print(f"  {'#':<3} {'Home':<22} {'Away':<22} {'Odds':<16} {'Signal':<30} {'Conf':<8}")
    print(f"  {'-'*95}")
    
    for i, pred in enumerate(predictions, 1):
        # Get odds display
        m = next_matches[i-1] if i-1 < len(next_matches) else {}
        markets = m.get('markets', [])
        odds_str = '-'
        if markets and len(markets[0].get('outcomes', [])) >= 3:
            outcomes = markets[0]['outcomes']
            odds_str = f"{float(outcomes[0]['odd_value']):.2f}/{float(outcomes[1]['odd_value']):.2f}/{float(outcomes[2]['odd_value']):.2f}"
        
        ht = pred['home']
        at = pred['away']
        signal = pred['signal']
        conf = pred['confidence']
        print(f"  M{i:<2} {ht:<22} {at:<22} {odds_str:<16} {signal:<30} {conf:<8}")
    
    print(f"\n{'='*90}\n")

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    print("🔮 VFL Prediction Engine v2")
    print("   Polling betkraft for live rounds + predicting next round\n")
    
    seen = set()
    prev_features = None
    next_odds = []
    standings = {}
    last_rid = None
    cycle = 0
    
    while True:
        try:
            periods = get_periods()
            now = datetime.now(timezone.utc)
            
            for period in periods:
                rid = period['round_number_id']
                if rid in seen:
                    continue
                
                start = to_utc(period['start_time'])
                wait = (start - now).total_seconds()
                
                # If round is in the future, show predictions first
                if wait > 0:
                    if prev_features and next_odds:
                        rules = load_rules()
                        predictions = predict_round(prev_features, next_odds, standings, rules)
                        display_round(last_rid, prev_features, next_odds, predictions)
                    print(f"  ⏳ Round #{rid} starts in {wait:.0f}s...")
                    time.sleep(min(wait + 2, BET_WINDOW))
                
                # Poll for results
                for attempt in range(15):
                    live = get_live(period)
                    if live and len(live) == 10:
                        seen.add(rid)
                        last_rid = rid
                        
                        # Parse and extract features
                        matches = sorted(live, key=lambda m: m['event_id'])
                        standings = get_standings()
                        
                        prev_features = extract_features(matches, standings)
                        
                        # Store home/away names in features for display
                        for m in matches:
                            n = m['event_id']  # N is position, not event_id
                        for i, m in enumerate(matches, 1):
                            prev_features[f'M{i}_home_team'] = m['home_team']
                            prev_features[f'M{i}_away_team'] = m['away_team']
                        
                        # Fetch next round odds
                        next_odds = get_next_odds()
                        if next_odds:
                            # Sort by event_id
                            next_odds.sort(key=lambda x: x['event_id'])
                        
                        # Display results + predictions together
                        rules = load_rules()
                        predictions = predict_round(prev_features, next_odds, standings, rules)
                        display_round(rid, prev_features, next_odds, predictions)
                        
                        print(f"  ⏳ Waiting for next round...\n")
                        break
                    time.sleep(2)
                else:
                    seen.add(rid)
                break  # Only process one period per cycle
            
            cycle += 1
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            print("\n🛑 Stopped")
            break
        except Exception as e:
            print(f"\n⚠️ Error: {e}")
            time.sleep(10)

if __name__ == '__main__':
    main()
