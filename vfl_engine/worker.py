"""VFL Engine Worker — runs learner, predictor, auditor cycles per source."""
import os, sys, json, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from vfl_engine.learner import init_tables, run_cycle as learn_cycle
from vfl_engine.predictor import predict_round, run_audit, clean_old_rules
from vfl_engine.config.markets import SOURCES

DB = os.environ.get("DATABASE_URL", "")
if not DB:
    print("ERROR: DATABASE_URL not set", flush=True)
    sys.exit(1)


def worker_cycle(limit_rounds=50):
    """
    One complete worker cycle:
    1. Check which sources have rounds
    2. For each source: mine rules → predict → audit
    3. Clean old failed rules
    """
    t0 = time.time()
    print(f"\n{'='*50}", flush=True)
    print(f"[vfl] Engine cycle starting at {datetime.now():%H:%M:%S}", flush=True)
    
    # Ensure tables exist
    init_tables()
    
    total_rules = 0
    total_preds = 0
    total_audits = 0
    
    for source in SOURCES:
        try:
            # Step 1: Mine dimension rules
            rules = learn_cycle(source, limit=limit_rounds)
            total_rules += rules
            
            if rules > 0:
                # Step 2: Predict using stacked engine
                preds = predict_round(source, limit=limit_rounds)
                total_preds += preds
                
                # Step 3: Audit previous predictions
                audits = run_audit(source, limit=100)
                total_audits += audits
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[vfl] {source} cycle FAILED: {e}", flush=True)
    
    # Clean old failed rules every cycle (48h threshold)
    try:
        clean_old_rules(max_age_hours=48)
    except Exception as e:
        print(f"[vfl] rule cleanup FAILED: {e}", flush=True)
    
    elapsed = time.time() - t0
    print(f"[vfl] Cycle done: {total_rules} rules, {total_preds} predictions, "
          f"{total_audits} audits in {elapsed:.0f}s", flush=True)
    print('='*50, flush=True)
    
    return {"rules": total_rules, "preds": total_preds, "audits": total_audits, "elapsed": elapsed}


def print_status():
    """Print a quick status of the engine tables."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM dim_rules")
    rules = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) FROM stack_predictions")
    preds = cur.fetchone()['count']
    
    cur.execute("""
        SELECT COUNT(*) FROM stack_predictions sp
        JOIN audit_log al ON al.prediction_id = sp.id
    """)
    audited = cur.fetchone()['count']
    
    conn.close()
    
    print(f"\n  dim_rules:       {rules}")
    print(f"  stack_predictions: {preds}")
    print(f"  audited:         {audited}")


if __name__ == '__main__':
    print("[vfl] VFL Engine Worker started", flush=True)
    init_tables()
    
    while True:
        try:
            worker_cycle()
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[vfl] Cycle FAILED: {e}, sleeping 60s", flush=True)
            time.sleep(60)
            continue
        
        # Sleep between cycles
        time.sleep(180)  # 3 min between cycles for testing
