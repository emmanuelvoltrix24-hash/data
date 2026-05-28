"""
VFL Unified Database Schema
Per-source tables + generic future tables + unified views.
Run `python3 schema.py` to initialize.
All rounds tables have chain_break + session_id for gap detection.
"""
import sqlite3, os
from datetime import datetime

DB_PATH = '/home/voltrix/vfl_data/vfl.db'

SCHEMA_SQL = """

-- ============================================================
-- 0. COLLECTOR REGISTRY
-- ============================================================
CREATE TABLE IF NOT EXISTS collector_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL UNIQUE,
    display_name TEXT,
    version TEXT,
    api_base TEXT,
    active BOOLEAN DEFAULT 1,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

INSERT OR IGNORE INTO collector_registry (source, display_name, version, api_base, notes) VALUES
    ('betkraft',   'Betkraft',   '1.0', 'https://vl.betkraft.co.uk',       '10 matches, 27 markets, standings'),
    ('bongobongo', 'BongoBongo', '1.0', 'https://vgp.sociumhubeurope.com', '10 matches, 1X2 odds, standings'),
    ('betpawa',    'BetPawa',    '1.0', 'https://www.betpawa.ug',          '66 events, 5 markets, 7 leagues'),
    ('bangbet',    'BangBet',    '1.0', 'https://bet-api.bangbet.com',     'Results + HT, 8 tournaments'),
    ('bet22',      '22Bet',      '1.0', 'https://22bet.ug',                'Full odds only'),
    ('other_1',    'Future',     NULL,  NULL,                              'Reserved'),
    ('other_2',    'Future',     NULL,  NULL,                              'Reserved'),
    ('other_3',    'Future',     NULL,  NULL,                              'Reserved');

-- ============================================================
-- 0b. GENERIC TABLES — for any future collector
-- ============================================================
CREATE TABLE IF NOT EXISTS generic_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    round_id TEXT NOT NULL,
    league TEXT,
    collected_at TIMESTAMP,
    has_results BOOLEAN DEFAULT 0,
    has_odds BOOLEAN DEFAULT 0,
    has_standings BOOLEAN DEFAULT 0,
    match_count INTEGER,
    raw_json TEXT,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(source, round_id)
);

CREATE TABLE IF NOT EXISTS generic_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    round_id TEXT NOT NULL,
    match_n INTEGER,
    home_team TEXT,
    away_team TEXT,
    hg INTEGER,
    ag INTEGER,
    result TEXT,
    outcome TEXT,
    parity TEXT,
    h_odd REAL,
    d_odd REAL,
    a_odd REAL,
    odds_json TEXT,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS generic_standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    round_id TEXT NOT NULL,
    position INTEGER,
    team TEXT,
    played INTEGER,
    points INTEGER,
    wins INTEGER,
    draws INTEGER,
    losses INTEGER,
    gf INTEGER,
    ga INTEGER,
    gd INTEGER,
    extra_json TEXT
);

-- ============================================================
-- 1. BETKRAFT
-- ============================================================
CREATE TABLE IF NOT EXISTS betkraft_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL UNIQUE,
    season_id INTEGER,
    competition_id INTEGER DEFAULT 1,
    league TEXT DEFAULT 'English',
    collected_at TIMESTAMP,
    has_odds BOOLEAN DEFAULT 0,
    has_standings BOOLEAN DEFAULT 0,
    match_count INTEGER DEFAULT 10,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(round_id)
);

CREATE TABLE IF NOT EXISTS betkraft_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    match_n INTEGER NOT NULL,
    event_id INTEGER,
    home_team TEXT,
    away_team TEXT,
    hg INTEGER,
    ag INTEGER,
    result TEXT,
    outcome TEXT CHECK(outcome IN ('W','D','L')),
    parity TEXT CHECK(parity IN ('E','O')),
    ht TEXT,
    home_score_times TEXT,
    away_score_times TEXT,
    FOREIGN KEY(round_id) REFERENCES betkraft_rounds(round_id)
);

CREATE TABLE IF NOT EXISTS betkraft_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    event_id INTEGER,
    market_id TEXT NOT NULL,
    outcome_id TEXT,
    outcome_name TEXT,
    odd_value REAL,
    FOREIGN KEY(round_id) REFERENCES betkraft_rounds(round_id)
);

CREATE TABLE IF NOT EXISTS betkraft_standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    position INTEGER,
    team_name TEXT,
    team TEXT,
    team_id INTEGER,
    points INTEGER,
    played INTEGER,
    wins INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    gf INTEGER DEFAULT 0,
    ga INTEGER DEFAULT 0,
    gd INTEGER DEFAULT 0,
    team_form TEXT,
    FOREIGN KEY(round_id) REFERENCES betkraft_rounds(round_id)
);

-- ============================================================
-- 2. BONGOBONGO
-- ============================================================
CREATE TABLE IF NOT EXISTS bongobongo_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_id INTEGER NOT NULL UNIQUE,
    week INTEGER,
    season INTEGER,
    day TEXT,
    timestamp TEXT,
    collected_at TIMESTAMP,
    has_results BOOLEAN DEFAULT 0,
    has_odds BOOLEAN DEFAULT 0,
    has_standings BOOLEAN DEFAULT 0,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(matchday_id)
);

CREATE TABLE IF NOT EXISTS bongobongo_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_id INTEGER NOT NULL,
    match_n INTEGER NOT NULL,
    match_id TEXT,
    home_team TEXT,
    away_team TEXT,
    home_id TEXT,
    away_id TEXT,
    hg INTEGER,
    ag INTEGER,
    result TEXT,
    outcome TEXT CHECK(outcome IN ('W','D','L')),
    parity TEXT CHECK(parity IN ('E','O')),
    h_odd REAL,
    d_odd REAL,
    a_odd REAL,
    h_pos INTEGER,
    a_pos INTEGER,
    FOREIGN KEY(matchday_id) REFERENCES bongobongo_rounds(matchday_id)
);

CREATE TABLE IF NOT EXISTS bongobongo_standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_id INTEGER NOT NULL,
    position INTEGER,
    team TEXT,
    team_id TEXT,
    played INTEGER,
    points INTEGER,
    wins INTEGER,
    draws INTEGER,
    losses INTEGER,
    gf INTEGER,
    ga INTEGER,
    gd INTEGER,
    FOREIGN KEY(matchday_id) REFERENCES bongobongo_rounds(matchday_id)
);

-- ============================================================
-- 3. BETPAWA
-- ============================================================
CREATE TABLE IF NOT EXISTS betpawa_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL UNIQUE,
    season TEXT,
    collected_at TIMESTAMP,
    has_results BOOLEAN DEFAULT 0,
    has_odds BOOLEAN DEFAULT 0,
    event_count INTEGER DEFAULT 66,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(round_id)
);

CREATE TABLE IF NOT EXISTS betpawa_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    home_team TEXT,
    away_team TEXT,
    league TEXT,
    league_id TEXT,
    hg INTEGER,
    ag INTEGER,
    result TEXT,
    collected_at TIMESTAMP,
    odd_1 REAL,
    odd_X REAL,
    odd_2 REAL,
    btts_yes REAL,
    btts_no REAL,
    dc_1x REAL,
    dc_x2 REAL,
    dc_12 REAL,
    ou_data TEXT,
    htft_data TEXT,
    FOREIGN KEY(round_id) REFERENCES betpawa_rounds(round_id)
);

CREATE TABLE IF NOT EXISTS betpawa_standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    league TEXT NOT NULL,
    league_id TEXT,
    position INTEGER,
    team TEXT,
    played INTEGER,
    points INTEGER,
    wins INTEGER,
    draws INTEGER,
    losses INTEGER,
    gf INTEGER,
    ga INTEGER,
    gd INTEGER,
    FOREIGN KEY(round_id) REFERENCES betpawa_rounds(round_id)
);

-- ============================================================
-- 4. BANGBET
-- ============================================================
CREATE TABLE IF NOT EXISTS bangbet_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_time INTEGER NOT NULL,
    tournament TEXT NOT NULL,
    tournament_id TEXT,
    timestamp TEXT,
    collected_at TIMESTAMP,
    match_count INTEGER,
    has_odds BOOLEAN DEFAULT 0,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(schedule_time, tournament)
);

CREATE TABLE IF NOT EXISTS bangbet_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_time INTEGER NOT NULL,
    tournament TEXT NOT NULL,
    match_n INTEGER,
    home_team TEXT,
    away_team TEXT,
    hg INTEGER,
    ag INTEGER,
    result TEXT,
    outcome TEXT CHECK(outcome IN ('W','D','L')),
    parity TEXT CHECK(parity IN ('E','O')),
    ht_home INTEGER,
    ht_away INTEGER
);

CREATE TABLE IF NOT EXISTS bangbet_standings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_time INTEGER NOT NULL,
    tournament TEXT NOT NULL,
    position INTEGER,
    team TEXT,
    played INTEGER,
    points INTEGER,
    wins INTEGER,
    draws INTEGER,
    losses INTEGER,
    gf INTEGER,
    ga INTEGER,
    gd INTEGER
);

CREATE TABLE IF NOT EXISTS bangbet_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_time INTEGER NOT NULL,
    tournament TEXT NOT NULL,
    match_n INTEGER,
    home_team TEXT,
    away_team TEXT,
    market_data TEXT,
    collected_at TIMESTAMP
);

-- ============================================================
-- 5. 22BET
-- ============================================================
CREATE TABLE IF NOT EXISTS bet22_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL UNIQUE,
    league TEXT,
    collected_at TIMESTAMP,
    match_count INTEGER,
    session_id TEXT,
    chain_break BOOLEAN DEFAULT 1,
    UNIQUE(round_id)
);

CREATE TABLE IF NOT EXISTS bet22_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    match_id INTEGER,
    match_n INTEGER,
    home_team TEXT,
    away_team TEXT,
    odd_1 REAL,
    odd_X REAL,
    odd_2 REAL,
    dc_1x REAL,
    dc_x2 REAL,
    dc_12 REAL,
    btts_yes REAL,
    btts_no REAL,
    ou_data TEXT,
    handicap_data TEXT,
    ind_total_data TEXT,
    FOREIGN KEY(round_id) REFERENCES bet22_rounds(round_id)
);

-- ============================================================
-- 6. UNIFIED VIEWS
-- ============================================================
CREATE VIEW IF NOT EXISTS v_all_rounds AS
SELECT 'betkraft' as source, CAST(round_id AS TEXT) as round_id, league, collected_at, match_count,
       chain_break, session_id FROM betkraft_rounds
UNION ALL
SELECT 'bongobongo', CAST(matchday_id AS TEXT), 'English', collected_at, 10,
       chain_break, session_id FROM bongobongo_rounds
UNION ALL
SELECT 'betpawa', round_id, 'Mixed(7)', collected_at, event_count,
       chain_break, session_id FROM betpawa_rounds
UNION ALL
SELECT 'bangbet', CAST(schedule_time AS TEXT), tournament, collected_at, match_count,
       chain_break, session_id FROM bangbet_rounds
UNION ALL
SELECT '22bet', round_id, league, collected_at, match_count,
       chain_break, session_id FROM bet22_rounds
UNION ALL
SELECT source, round_id, league, collected_at, match_count,
       chain_break, session_id FROM generic_rounds;

CREATE VIEW IF NOT EXISTS v_all_matches AS
SELECT 'betkraft' as source, CAST(r.round_id AS TEXT) as round_id, m.match_n,
       m.home_team, m.away_team, m.hg, m.ag, m.result, m.outcome, m.parity,
       m.ht as ht_score, NULL as league
FROM betkraft_matches m JOIN betkraft_rounds r ON m.round_id = r.round_id
UNION ALL
SELECT 'bongobongo', CAST(r.matchday_id AS TEXT), m.match_n, m.home_team, m.away_team,
       m.hg, m.ag, m.result, m.outcome, m.parity, NULL, NULL
FROM bongobongo_matches m JOIN bongobongo_rounds r ON m.matchday_id = r.matchday_id
UNION ALL
SELECT 'betpawa', e.round_id, 0, e.home_team, e.away_team,
       e.hg, e.ag, e.result,
       CASE WHEN e.hg > e.ag THEN 'W' WHEN e.hg = e.ag THEN 'D' ELSE 'L' END,
       CASE WHEN (e.hg + e.ag) % 2 = 0 THEN 'E' ELSE 'O' END,
       NULL, e.league
FROM betpawa_events e WHERE e.result IS NOT NULL
UNION ALL
SELECT 'bangbet', CAST(m.schedule_time AS TEXT), m.match_n, m.home_team, m.away_team,
       m.hg, m.ag, m.result, m.outcome, m.parity,
       CAST(m.ht_home AS TEXT) || ':' || CAST(m.ht_away AS TEXT), m.tournament
FROM bangbet_matches m
UNION ALL
SELECT source, round_id, match_n, home_team, away_team,
       hg, ag, result, outcome, parity, NULL, NULL
FROM generic_matches
ORDER BY round_id, match_n;

CREATE VIEW IF NOT EXISTS v_all_standings AS
SELECT 'betkraft' as source, CAST(r.round_id AS TEXT) as round_id, s.position,
       s.team_name as team, s.points, s.played, s.wins, s.draws, s.losses, s.gf, s.ga, s.gd
FROM betkraft_standings s JOIN betkraft_rounds r ON s.round_id = r.round_id
UNION ALL
SELECT 'bongobongo', CAST(r.matchday_id AS TEXT), s.position, s.team,
       s.points, s.played, s.wins, s.draws, s.losses, s.gf, s.ga, s.gd
FROM bongobongo_standings s JOIN bongobongo_rounds r ON s.matchday_id = r.matchday_id
UNION ALL
SELECT 'betpawa', s.round_id, s.position, s.team,
       s.points, s.played, s.wins, s.draws, s.losses, s.gf, s.ga, s.gd
FROM betpawa_standings s
UNION ALL
SELECT 'bangbet', CAST(s.schedule_time AS TEXT), s.position, s.team,
       s.points, s.played, s.wins, s.draws, s.losses, s.gf, s.ga, s.gd
FROM bangbet_standings s
UNION ALL
SELECT source, round_id, position, team,
       points, played, wins, draws, losses, gf, ga, gd
FROM generic_standings;

CREATE VIEW IF NOT EXISTS v_parity_patterns AS
SELECT m.round_id, m.source, m.match_n, m.home_team, m.away_team,
       m.result, m.outcome, m.parity
FROM v_all_matches m
WHERE m.match_n IN (5, 6, 7, 10)
ORDER BY m.round_id, m.match_n;

CREATE VIEW IF NOT EXISTS v_chain_breaks AS
SELECT source, round_id, collected_at, session_id FROM v_all_rounds
WHERE chain_break = 1
ORDER BY source, collected_at;

""".strip()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
    views = [r[0] for r in cur.fetchall()]
    cur = conn.execute("SELECT source, display_name FROM collector_registry WHERE active=1")
    sources = [f"{r[0]} ({r[1]})" for r in cur.fetchall()]
    conn.close()

    print(f"✅ DB: {DB_PATH}")
    print(f"   Tables: {len(tables)}")
    print(f"   Views:  {len(views)}  (v_chain_breaks shows restarts)")
    print(f"   Sources: {len(sources)}")
    print(f"   Gap detection: chain_break=1 on first round of each run")


if __name__ == '__main__':
    init_db()
