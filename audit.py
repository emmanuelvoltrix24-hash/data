#!/usr/bin/env python3
"""
VFL Prediction Auditor
Compares stored predictions against actual results once they arrive.
Tracks accuracy per slot/target/source over time.
"""
import os, sys, json, time, math
from datetime import datetime, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

DB_URL = os.environ.get('DATABASE_URL', '')

def get_db():
    import psycopg2
    return psycopg2.connect(DB_URL)

def ensure_tables():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            prediction_id INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
            round_id TEXT NOT NULL,
            source TEXT NOT NULL,
            slot TEXT NOT NULL,
            target TEXT NOT NULL,
            pred_type TEXT,
            pred_val TEXT,
            actual_val TEXT,
            was_correct BOOLEAN,
            precision_at_time FLOAT,
            checked_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_round ON audit_log(round_id, source)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_slot ON audit_log(slot, pred_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_correct ON audit_log(was_correct)")
    conn.commit()
    conn.close()

def get_prediction_actual(features, pred_type, pred_val):
    """Given a round's features and a prediction, get the actual value."""
    # Find the slot from the features
    # pred_type could be parity, cs, outcome
    # We need M{slot}_{type} from features
    return None  # caller will iterate slots

def get_unchecked_predictions(limit=100):
    """Get predictions that haven't been audited yet."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.round_id, p.source, p.slot, p.target, 
               p.pred_type, p.pred_val, p.precision, p.features::text
        FROM predictions p
        LEFT JOIN audit_log a ON a.prediction_id = p.id
        WHERE a.id IS NULL
        ORDER BY p.created_at DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{
        'id': r[0], 'round_id': r[1], 'source': r[2], 'slot': r[3],
        'target': r[4], 'pred_type': r[5], 'pred_val': r[6],
        'precision': r[7], 'features': r[8],
    } for r in rows]

def check_audit(limit=100):
    """Audit unchecked predictions against actual results."""
    unchecked = get_unchecked_predictions(limit)
    if not unchecked:
        return 0
    
    conn = get_db()
    cur = conn.cursor()
    
    # Group by round_id + source so we fetch each round once
    rounds_needed = {}
    for p in unchecked:
        key = (p['round_id'], p['source'])
        rounds_needed.setdefault(key, []).append(p)
    
    checked = 0
    for (rid, source), predictions in rounds_needed.items():
        # Fetch the round data
        cur.execute("SELECT data::text FROM rounds WHERE source=%s AND round_id::text=%s LIMIT 1", (source, rid))
        row = cur.fetchone()
        if not row:
            continue
        
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        matches = data.get('matches', [])
        if not matches:
            continue
        
        # Build a lookup of actual values per slot
        actuals = {}  # slot -> {parity, cs, outcome, ht_parity, ht_outcome}
        for m in matches:
            n = m.get('n', 0)
            hg = int(m.get('hg', 0))
            ag = int(m.get('ag', 0))
            total = hg + ag
            ht = m.get('ht', '')
            
            slot = f'M{n}'
            par = 'E' if total % 2 == 0 else 'O' if total > 0 else None
            cs = (hg == 0 or ag == 0)
            outcome = 'W' if hg > ag else ('L' if hg < ag else 'D')
            
            actuals[slot] = {
                'parity': par,
                'cs': str(cs),
                'outcome': outcome,
            }
            
            # HT
            if ht and ':' in str(ht):
                parts = str(ht).split(':')
                ht_hg, ht_ag = int(parts[0]), int(parts[1])
                ht_total = ht_hg + ht_ag
                actuals[slot]['ht_parity'] = 'E' if ht_total % 2 == 0 else 'O' if ht_total > 0 else None
                actuals[slot]['ht_outcome'] = 'W' if ht_hg > ht_ag else ('L' if ht_hg < ht_ag else 'D')
        
        # Check each prediction
        for p in predictions:
            slot = p['slot']
            pred_type = p['pred_type']
            pred_val = p['pred_val']
            
            if slot not in actuals:
                continue
            
            actual_val = actuals[slot].get(pred_type)
            
            if actual_val is None:
                continue
            
            # Compare
            was_correct = (str(actual_val) == str(pred_val))
            
            cur.execute("""
                INSERT INTO audit_log 
                (prediction_id, round_id, source, slot, target, pred_type, pred_val, actual_val, was_correct, precision_at_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (p['id'], rid, source, slot, p['target'], pred_type, pred_val, str(actual_val), was_correct, p['precision']))
            checked += 1
    
    conn.commit()
    conn.close()
    return checked

def audit_summary():
    """Get audit stats."""
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT 
            pred_type,
            COUNT(*) as total,
            SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
            ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY pred_type
        ORDER BY total DESC
    """)
    print("=== AUDIT SUMMARY BY TYPE ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<10} {r[1]:>5} total  {r[2]:>5} correct  {r[3]:>6}% accuracy")
    
    cur.execute("""
        SELECT 
            slot,
            COUNT(*) as total,
            SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
            ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY slot
        ORDER BY slot
    """)
    print("\n=== AUDIT SUMMARY BY SLOT ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<5} {r[1]:>5} total  {r[2]:>5} correct  {r[3]:>6}% accuracy")
    
    cur.execute("""
        SELECT 
            target,
            COUNT(*) as total,
            SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
            ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY target
        HAVING COUNT(*) >= 3
        ORDER BY accuracy_pct DESC
        LIMIT 15
    """)
    print("\n=== TOP ACCURATE TARGETS (≥3 checks) ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<30} {r[1]:>3} checks  {r[2]:>3} correct  {r[3]:>6}%")
    
    cur.execute("""
        SELECT 
            target,
            COUNT(*) as total,
            SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
            ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY target
        HAVING COUNT(*) >= 3
        ORDER BY accuracy_pct ASC
        LIMIT 5
    """)
    print("\n=== WORST TARGETS (≥3 checks) ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<30} {r[1]:>3} checks  {r[2]:>3} correct  {r[3]:>6}%")
    
    # Best markets per source (most accurate prediction types per source)
    cur.execute("""
        SELECT 
            source,
            pred_type,
            COUNT(*) as total,
            SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
            ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY source, pred_type
        HAVING COUNT(*) >= 3
        ORDER BY source, accuracy_pct DESC
    """)
    best_markets = {}
    for r in cur.fetchall():
        src = r[0]
        if src not in best_markets:
            best_markets[src] = []
        best_markets[src].append({
            'market': r[1],
            'total': r[2],
            'correct': r[3],
            'accuracy': r[4],
        })
    
    print("\n=== BEST MARKETS PER SOURCE ===")
    for src, markets in best_markets.items():
        print(f"  {src}:")
        for m in markets[:5]:
            print(f"    {m['market']:<10} {m['total']:>4} checks  {m['correct']:>4} correct  {m['accuracy']:>6}%")
    
    # Source ranking by accuracy
    cur.execute("""
        SELECT source,
               COUNT(*) as total,
               SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
               ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY source
        HAVING COUNT(*) >= 3
        ORDER BY accuracy_pct DESC
    """)
    print("\n=== SOURCE RANKING ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<12} {r[1]:>4} checks  {r[2]:>4} correct  {r[3]:>6}%")
    
    # Slot ranking by accuracy
    cur.execute("""
        SELECT slot,
               COUNT(*) as total,
               SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct,
               ROUND(100.0 * SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as accuracy_pct
        FROM audit_log
        GROUP BY slot
        HAVING COUNT(*) >= 3
        ORDER BY accuracy_pct DESC
    """)
    print("\n=== SLOT RANKING ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<5} {r[1]:>4} checks  {r[2]:>4} correct  {r[3]:>6}%")
    
    cur.execute("SELECT COUNT(*) FROM audit_log")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM predictions")
    preds = cur.fetchone()[0]
    print(f"\n=== OVERALL ===")
    print(f"  Predictions: {preds}")
    print(f"  Audited: {total} ({100*total/max(preds,1):.0f}%)")
    cur.execute("SELECT COUNT(*) FROM audit_log WHERE was_correct=true")
    correct = cur.fetchone()[0]
    print(f"  Correct: {correct} ({100*correct/max(total,1):.1f}%)")
    
    conn.close()
    return {
        'total_audited': total,
        'total_correct': correct,
        'accuracy_pct': round(100*correct/max(total,1), 1) if total else 0,
        'best_markets_per_source': best_markets,
    }

def main():
    print(f"📊 VFL Auditor started [{datetime.now().strftime('%H:%M:%S')}]")
    ensure_tables()
    
    while True:
        try:
            checked = check_audit(limit=200)
            if checked:
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] Audited {checked} predictions", flush=True)
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n🛑 Stopped")
            break
        except Exception as e:
            print(f"⚠️ {e}", flush=True)
            time.sleep(30)

if __name__ == '__main__':
    if '--summary' in sys.argv:
        audit_summary()
    else:
        main()
