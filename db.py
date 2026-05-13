#!/usr/bin/env python3
"""
Unified DB writer for all VFL collectors.
All collectors import save_round() and save_odds_snapshot() from here.
"""
import os, json
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get('DATABASE_URL', '')


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            # Main rounds table — one row per (source, round_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rounds (
                    id SERIAL PRIMARY KEY,
                    round_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    league TEXT,
                    collected_at TIMESTAMP,
                    has_odds BOOLEAN DEFAULT FALSE,
                    has_standings BOOLEAN DEFAULT FALSE,
                    has_ht BOOLEAN DEFAULT FALSE,
                    data JSONB,
                    UNIQUE(source, round_id)
                )
            """)
            # Odds comparison — one row per (round_id, match_n) across sources
            cur.execute("""
                CREATE TABLE IF NOT EXISTS odds_comparison (
                    id SERIAL PRIMARY KEY,
                    round_id TEXT,
                    match_n INT,
                    home_team TEXT,
                    away_team TEXT,
                    source TEXT,
                    h_odd FLOAT,
                    d_odd FLOAT,
                    a_odd FLOAT,
                    collected_at TIMESTAMP,
                    UNIQUE(round_id, match_n, source)
                )
            """)
            # Rules table (used by learner)
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
    print("DB initialized")


def save_round(round_id, source, league, matches, standings=None, extra=None):
    """
    Save a round from any collector.
    matches: list of dicts with at minimum {n, home, away, hg, ag, result}
    standings: list of dicts (optional)
    extra: any additional fields to store in data JSONB
    """
    has_odds     = any(m.get('odds') for m in matches)
    has_standings = bool(standings)
    has_ht       = any(m.get('ht') for m in matches)

    data = {
        'round_id':    round_id,
        'source':      source,
        'league':      league,
        'collected_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'matches':     matches,
        'standings':   standings or [],
        **(extra or {}),
    }

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rounds (round_id, source, league, collected_at, has_odds, has_standings, has_ht, data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, round_id) DO UPDATE
                SET data = EXCLUDED.data,
                    has_odds = EXCLUDED.has_odds,
                    has_standings = EXCLUDED.has_standings,
                    has_ht = EXCLUDED.has_ht,
                    collected_at = EXCLUDED.collected_at
            """, (str(round_id), source, league, datetime.now(),
                  has_odds, has_standings, has_ht, json.dumps(data)))

            # Write odds comparison rows
            for m in matches:
                odds = m.get('odds', {})
                x2 = odds.get('1x2') or odds.get('1X2') or {}
                h = x2.get('1') or x2.get('H')
                d = x2.get('X') or x2.get('D')
                a = x2.get('2') or x2.get('A')
                if h and d and a:
                    cur.execute("""
                        INSERT INTO odds_comparison
                        (round_id, match_n, home_team, away_team, source, h_odd, d_odd, a_odd, collected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (round_id, match_n, source) DO NOTHING
                    """, (str(round_id), m['n'], m.get('home',''), m.get('away',''),
                          source, float(h), float(d), float(a), datetime.now()))
        conn.commit()


def get_seen_ids(source):
    """Return set of already-collected round_ids for a given source."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT round_id FROM rounds WHERE source = %s", (source,))
            return {r['round_id'] for r in cur.fetchall()}


if __name__ == '__main__':
    init_db()
    print("Schema ready")
