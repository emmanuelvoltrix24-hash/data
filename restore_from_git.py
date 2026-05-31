#!/usr/bin/env python3
"""Restore DB tables from the latest git backup files."""

import gzip, json, os, datetime, glob

DB = os.environ.get('DATABASE_URL', '')
BACKUP_DIR = 'backups'
TABLES = ['global_rules', 'rounds', 'predictions', 'audit_log']

def restore():
    if not DB:
        print("[restore] No DATABASE_URL -- skipping")
        return
    import psycopg2
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    repo_root = os.path.dirname(os.path.abspath(__file__))

    for table in TABLES:
        pattern = os.path.join(repo_root, BACKUP_DIR, f"{table}_*.json.gz")
        files = sorted(glob.glob(pattern))
        if not files:
            print(f"[restore] {table}: no backup files found")
            continue
        latest = files[-1]
        print(f"[restore] {table}: loading {latest}")

        with gzip.open(latest, 'rt', encoding='utf-8') as f:
            data = json.load(f)

        rows = data['rows']
        if not rows:
            print(f"[restore] {table}: empty")
            continue

        cols = data['columns']
        placeholders = ','.join(['%s'] * len(cols))
        colnames = ','.join(cols)

        # Clear and re-insert
        cur.execute(f'TRUNCATE TABLE "{table}"')
        chunk_size = 500
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i+chunk_size]
            vals = [[r.get(c) for c in cols] for r in chunk]
            for v in vals:
                for j, x in enumerate(v):
                    if isinstance(x, str) and len(x) > 1000:
                        v[j] = x[:1000]  # safety truncate
            from psycopg2.extras import execute_values
            execute_values(cur, f'INSERT INTO "{table}" ({colnames}) VALUES %s', vals)

        conn.commit()
        print(f"[restore] {table}: {len(rows)} rows restored")

    cur.close()
    conn.close()
    print("[restore] Done.")

if __name__ == '__main__':
    restore()
