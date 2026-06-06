"""VFL Predictor — loads dim rules, stacks for current rounds, saves predictions."""
import os, json, time
from collections import defaultdict
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from vfl_engine.stacker import stack_round, validate_audit, DIMENSIONS
from vfl_engine.learner import load_rules, extract_features, normalize_match
from vfl_engine.config.markets import SOURCES

DB = os.environ.get("DATABASE_URL", "")


def get_conn():
    return psycopg2.connect(DB, cursor_factory=RealDictCursor)


def get_latest_round(source, limit=50):
    """Get recent rounds from DB for a source."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT round_id, data, source FROM rounds WHERE source=%s "
                "ORDER BY round_id DESC LIMIT %s",
                (source, limit))
            rows = cur.fetchall()
    return reversed(rows)


def predict_round(source, limit=50):
    """
    Full prediction cycle for a source:
    1. Load latest dim rules
    2. Load recent rounds
    3. Stack rules on each slot
    4. Save predictions
    """
    t0 = time.time()
    
    # Load rules
    dim_rules = load_rules(source)
    if not dim_rules:
        print(f"  {source}: no rules loaded", flush=True)
        return 0
    
    config = SOURCES.get(source, {})
    if not config:
        print(f"  {source}: no market config", flush=True)
        return 0
    
    # Load rounds
    rounds = list(get_latest_round(source, limit=limit))
    if not rounds:
        print(f"  {source}: no rounds", flush=True)
        return 0
    
    market_config = config.get("targets", {})
    predictions_saved = 0
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            for rd in rounds:
                round_id = rd['round_id']
                data = rd['data']
                if isinstance(data, str):
                    data = json.loads(data)
                
                matches = data.get('matches', [])
                if not matches:
                    continue
                
                # Extract features for each slot
                slot_features = {}
                for m in matches:
                    n = int(m.get('n', 0))
                    norm = normalize_match(m, source)
                    slot_features[n] = {
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
                
                # Stack predictions
                results = stack_round(slot_features, source, dim_rules, config)
                
                # Save to stack_predictions
                for slot_num, result in results.items():
                    # Build the prediction entry
                    pred_data = {
                        'top_scores': result['top_scores'],
                        'top_score_str': result['top_score_str'],
                        'top_prob': result['top_prob'],
                        'derived': result['derived_bettable'],
                        'source': source,
                        'round_id': round_id,
                        'slot': slot_num,
                    }
                    
                    cur.execute("""
                        INSERT INTO stack_predictions
                        (round_id, source, slot, top3, derived, top_prob, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        round_id, source, int(slot_num),
                        json.dumps(result['top_scores']),
                        json.dumps(result['derived_bettable']),
                        result['top_prob'],
                        datetime.now()
                    ))
                    predictions_saved += 1
                
                # Also save to old predictions table for backward compat
                for slot_num, result in results.items():
                    top = result['top_scores'][0] if result['top_scores'] else (0, 0, 0)
                    for target_key, target_val in result['derived_bettable'].items():
                        target_str = f"M{slot_num}_{target_key}={target_val}"
                        cur.execute("""
                            INSERT INTO predictions
                            (round_id, source, slot, target, pred_type, pred_val,
                             precision, confidence, created_at, conditions)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            round_id, source, f"M{slot_num}",
                            target_str, target_key, str(target_val),
                            result['top_prob'], 'HIGH' if result['top_prob'] > 0.7 else 'MEDIUM',
                            datetime.now(),
                            json.dumps({'stack': result['top_scores'], 'source': source})
                        ))
            
            conn.commit()
    
    elapsed = time.time() - t0
    print(f"  {source}: {predictions_saved} stacked predictions in {elapsed:.1f}s", flush=True)
    return predictions_saved


def run_audit(source, limit=100):
    """
    Audit stacked predictions against actual results.
    For each prediction, check the actual match result and score.
    """
    t0 = time.time()
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Get stack predictions that haven't been audited yet
            cur.execute("""
                SELECT sp.id, sp.round_id, sp.slot, sp.top3, sp.top_prob,
                       r.data as round_data
                FROM stack_predictions sp
                JOIN rounds r ON r.round_id = sp.round_id AND r.source = sp.source
                WHERE sp.source = %s
                ORDER BY sp.id DESC
                LIMIT %s
            """, (source, limit))
            
            rows = cur.fetchall()
            if not rows:
                return 0
            
            audited = 0
            for row in rows:
                data = row['round_data']
                if isinstance(data, str):
                    data = json.loads(data)
                
                matches = data.get('matches', [])
                slot = row['slot']
                
                # Find match at this slot
                match = None
                for m in matches:
                    if int(m.get('n', 0)) == slot:
                        match = m
                        break
                
                if not match:
                    continue
                
                norm = normalize_match(match, source)
                actual_hg = norm['hg']
                actual_ag = norm['ag']
                
                # Get prediction
                top3 = row['top3']
                if isinstance(top3, str):
                    top3 = json.loads(top3)
                
                slot_result = {
                    'top_scores': top3,
                    'derived_all': {},
                }
                
                audit_result = validate_audit(slot_result, actual_hg, actual_ag)
                
                # Save to audit_log
                cur.execute("""
                    INSERT INTO audit_log
                    (prediction_id, round_id, source, slot, target, pred_type,
                     pred_val, actual_val, was_correct, precision_at_time, checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['id'], row['round_id'], source, str(slot),
                    f"M{slot}_score", 'cs',
                    str(top3), f"{actual_hg}-{actual_ag}",
                    audit_result['exact'], row['top_prob'],
                    datetime.now()
                ))
                audited += 1
            
            conn.commit()
    
    print(f"  {source}: {audited} audit entries in {time.time()-t0:.1f}s", flush=True)
    return audited


def clean_old_rules(max_age_hours=48):
    """Delete failed rules older than max_age_hours."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM global_failed_rules
                WHERE failed_at < NOW() - INTERVAL '%s hours'
            """, (str(max_age_hours),))
            deleted = cur.rowcount
            if deleted > 0:
                print(f"  Cleaned {deleted} old failed rules", flush=True)
        conn.commit()
