#!/usr/bin/env python3
"""Dump key DB tables to compressed JSON, commit to git, then TRUNCATE to free space."""

import gzip, json, os, subprocess, datetime

DB = os.environ.get('DATABASE_URL', '')
BACKUP_DIR = 'backups'
TABLES = ['global_rules', 'rounds', 'predictions', 'audit_log']
KEEP_ROUNDS = 200      # keep this many recent rounds for predictor features
KEEP_PREDS   = 500      # keep this many recent predictions for auditor
KEEP_RULES  = 10000     # keep top rules by EV for app display + predictor

def backup_and_cut():
    if not DB:
        print("[backup] No DATABASE_URL -- skipping")
        return

    import psycopg2
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
    repo_root = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(repo_root, BACKUP_DIR), exist_ok=True)

    # ── Dump all tables ──
    for table in TABLES:
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            count = cur.fetchone()[0]
            if count == 0:
                print(f"[backup] {table}: empty, skipping")
                continue
            cur.execute(f'SELECT * FROM "{table}"')
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in rows:
                for k, v in r.items():
                    if isinstance(v, (datetime.date, datetime.datetime)):
                        r[k] = v.isoformat()
                    if isinstance(v, bytes):
                        r[k] = v.hex()
            data = {"table": table, "count": count, "columns": cols, "rows": rows}
            fname = os.path.join(repo_root, BACKUP_DIR, f"{table}_{ts}.json.gz")
            with gzip.open(fname, 'wt', encoding='utf-8') as f:
                json.dump(data, f)
            sz = os.path.getsize(fname)
            print(f"[backup] {table}: {count} rows -> {fname} ({sz/1024:.1f} KB)")
        except Exception as e:
            print(f"[backup] {table}: dump error: {e}")

    # ── Commit to git ──
    try:
        subprocess.run(['git', '-C', repo_root, 'add', BACKUP_DIR], check=True, capture_output=True)
        subprocess.run(['git', '-C', repo_root, 'commit', '-m', f'backup: {ts} -- full dump'], check=True, capture_output=True)
        subprocess.run(['git', '-C', repo_root, 'push', 'origin', 'master'], timeout=60, check=True, capture_output=True)
        print("[backup] Committed and pushed to git", flush=True)
    except Exception as e:
        print(f"[backup] git error: {e} -- truncation skipped for safety", flush=True)
        conn.close()
        return

    # ── No truncation — 18 GB free on EC2, keep all data ──
    print("[backup] 20 GB disk — keeping all data in PostgreSQL", flush=True)

    conn.close()

if __name__ == '__main__':
    backup_and_cut()
