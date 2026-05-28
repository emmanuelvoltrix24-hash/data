"""
VFL Unified DB Writer — shared module for all collectors.
Each collector imports `db` and calls the appropriate save function.
Auto-tracks: session_id, chain_break (first save after restart = break).
"""
import sqlite3, os, json, uuid
from datetime import datetime

DB_PATH = '/home/voltrix/vfl_data/vfl.db'

# ── Session management ──────────────────────────────────────────
SESSION_ID = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
_has_written = {}  # source -> bool — tracks first write per source per session


def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _chain_break(source):
    """Returns True on the first DB write of this session for this source."""
    if source not in _has_written:
        _has_written[source] = False
        return True
    return False


def _now():
    return datetime.now().isoformat()


# ── Generic writer (for any current or future collector) ──────────

def save_generic_round(source, round_id, match_count, league=None,
                       has_results=False, has_odds=False, has_standings=False,
                       raw_json=None):
    """Save a round to generic_rounds. Works with ANY collector past or future."""
    conn = _get_conn()
    cb = 1 if _chain_break(source) else 0
    conn.execute("""
        INSERT OR REPLACE INTO generic_rounds
            (source, round_id, league, collected_at, has_results, has_odds,
             has_standings, match_count, raw_json, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (source, str(round_id), league, _now(), int(has_results),
          int(has_odds), int(has_standings), match_count,
          json.dumps(raw_json) if raw_json else None,
          SESSION_ID, cb))
    conn.commit()
    conn.close()
    return cb


def save_generic_matches(source, round_id, matches):
    """Save match list to generic_matches. Each match: {match_n, home_team, away_team, hg, ag, ...}"""
    conn = _get_conn()
    for m in matches:
        odds_json = json.dumps(m.get('odds', {})) if m.get('odds') else None
        extra = {k: v for k, v in m.items()
                 if k not in ('match_n', 'home_team', 'away_team', 'hg', 'ag',
                              'result', 'outcome', 'parity', 'h_odd', 'd_odd', 'a_odd', 'odds')}
        conn.execute("""
            INSERT INTO generic_matches
                (source, round_id, match_n, home_team, away_team, hg, ag,
                 result, outcome, parity, h_odd, d_odd, a_odd, odds_json, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, str(round_id), m.get('match_n'), m.get('home_team'), m.get('away_team'),
              m.get('hg'), m.get('ag'), m.get('result'), m.get('outcome'), m.get('parity'),
              m.get('h_odd'), m.get('d_odd'), m.get('a_odd'),
              odds_json, json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


def save_generic_standings(source, round_id, standings):
    """Save standings list to generic_standings."""
    conn = _get_conn()
    for s in standings:
        conn.execute("""
            INSERT INTO generic_standings
                (source, round_id, position, team, played, points,
                 wins, draws, losses, gf, ga, gd, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, str(round_id), s.get('pos', s.get('position')),
              s.get('team', s.get('team_name')),
              s.get('played'), s.get('points', s.get('pts')),
              s.get('w', s.get('wins')), s.get('d', s.get('draws')),
              s.get('l', s.get('losses')), s.get('gf'), s.get('ga'),
              s.get('gd'), None))
    conn.commit()
    conn.close()


# ── Betkraft writer ──────────────────────────────────────────────

def save_betkraft_round(round_id, season_id=None, competition_id=1, league='English',
                        matches=None, standings=None, has_odds=False):
    """Save a full betkraft round: round + matches + odds + standings."""
    conn = _get_conn()
    cb = 1 if _chain_break('betkraft') else 0

    conn.execute("""
        INSERT OR REPLACE INTO betkraft_rounds
            (round_id, season_id, competition_id, league, collected_at,
             has_odds, has_standings, match_count, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (round_id, season_id, competition_id, league, _now(),
          int(has_odds), int(bool(standings)),
          len(matches) if matches else 0, SESSION_ID, cb))

    if matches:
        for m in matches:
            conn.execute("""
                INSERT OR REPLACE INTO betkraft_matches
                    (round_id, match_n, event_id, home_team, away_team,
                     hg, ag, result, outcome, parity, ht,
                     home_score_times, away_score_times)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (round_id, m.get('n'), m.get('event_id'),
                  m.get('home_team'), m.get('away_team'),
                  m.get('hg'), m.get('ag'), m.get('result'),
                  m.get('outcome'), m.get('parity'), m.get('ht'),
                  json.dumps(m.get('home_score_times', [])),
                  json.dumps(m.get('away_score_times', []))))

            # Odds from pre_markets
            pre_mkts = m.get('pre_markets', {})
            for market_id, outcomes in pre_mkts.items():
                for o in outcomes:
                    conn.execute("""
                        INSERT INTO betkraft_odds
                            (round_id, event_id, market_id, outcome_id, outcome_name, odd_value)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (round_id, m.get('event_id'), market_id,
                          o.get('outcome_id'), o.get('outcome_name'),
                          o.get('odd_value')))

    if standings:
        for s in standings:
            conn.execute("""
                INSERT OR REPLACE INTO betkraft_standings
                    (round_id, position, team_name, team, team_id, points,
                     played, wins, draws, losses, gf, ga, gd, team_form)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (round_id, s.get('position'), s.get('team_name'),
                  s.get('team'), s.get('team_id'), s.get('points'),
                  s.get('played', 0),
                  s.get('w', 0), s.get('d', 0), s.get('l', 0),
                  s.get('gf', 0), s.get('ga', 0), s.get('gd', 0),
                  s.get('team_form', '')))

    conn.commit()
    conn.close()
    return cb


# ── BongoBongo writer ───────────────────────────────────────────

def save_bongobongo_round(matchday_id, week=None, season=None, day=None, timestamp=None,
                          matches=None, standings=None):
    """Save a full bongobongo round: round + matches + standings."""
    conn = _get_conn()
    cb = 1 if _chain_break('bongobongo') else 0
    has_odds = any(m.get('h_odd') for m in (matches or []))

    conn.execute("""
        INSERT OR REPLACE INTO bongobongo_rounds
            (matchday_id, week, season, day, timestamp, collected_at,
             has_results, has_odds, has_standings, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (matchday_id, week, season, day, timestamp, _now(),
          1, int(has_odds), int(bool(standings)), SESSION_ID, cb))

    if matches:
        for m in matches:
            conn.execute("""
                INSERT OR REPLACE INTO bongobongo_matches
                    (matchday_id, match_n, match_id, home_team, away_team,
                     home_id, away_id, hg, ag, result, outcome, parity,
                     h_odd, d_odd, a_odd, h_pos, a_pos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (matchday_id, m.get('n'), m.get('match_id'),
                  m.get('home'), m.get('away'), m.get('home_id'),
                  m.get('away_id'), m.get('hg'), m.get('ag'),
                  m.get('result'), m.get('outcome'), m.get('parity'),
                  m.get('h_odd'), m.get('d_odd'), m.get('a_odd'),
                  m.get('h_pos'), m.get('a_pos')))

    if standings:
        for s in standings:
            conn.execute("""
                INSERT OR REPLACE INTO bongobongo_standings
                    (matchday_id, position, team, team_id, played, points,
                     wins, draws, losses, gf, ga, gd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (matchday_id, s.get('pos'), s.get('team'),
                  s.get('team_id'), s.get('played'), s.get('points'),
                  s.get('w'), s.get('d'), s.get('l'),
                  s.get('gf'), s.get('ga'), s.get('gd')))

    conn.commit()
    conn.close()
    return cb


# ── BetPawa writer ──────────────────────────────────────────────

def save_betpawa_round(round_id, season=None, events=None, standings=None):
    """Save a full betpawa round: round + events + standings."""
    conn = _get_conn()
    cb = 1 if _chain_break('betpawa') else 0
    has_odds = any(e.get('odds', {}).get('1x2') for e in (events or []))
    has_results = any(e.get('result') for e in (events or []))

    conn.execute("""
        INSERT OR REPLACE INTO betpawa_rounds
            (round_id, season, collected_at, has_results, has_odds,
             event_count, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(round_id), season, _now(),
          int(has_results), int(has_odds),
          len(events) if events else 0, SESSION_ID, cb))

    if events:
        for e in events:
            mkts = e.get('markets', e.get('odds', {}))
            conn.execute("""
                INSERT OR REPLACE INTO betpawa_events
                    (round_id, event_id, home_team, away_team, league, league_id,
                     hg, ag, result, collected_at,
                     odd_1, odd_X, odd_2,
                     btts_yes, btts_no,
                     dc_1x, dc_x2, dc_12,
                     ou_data, htft_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(round_id), e.get('event_id'),
                  e.get('home_team'), e.get('away_team'),
                  e.get('league'), e.get('league_id'),
                  e.get('hg'), e.get('ag'), e.get('result'), _now(),
                  mkts.get('1x2', {}).get('1'),
                  mkts.get('1x2', {}).get('X'),
                  mkts.get('1x2', {}).get('2'),
                  mkts.get('btts', {}).get('yes'),
                  mkts.get('btts', {}).get('no'),
                  mkts.get('dc', {}).get('1x'),
                  mkts.get('dc', {}).get('x2'),
                  mkts.get('dc', {}).get('12'),
                  json.dumps(mkts.get('ou', [])),
                  json.dumps(mkts.get('htft', {}))))

    if standings:
        for league_name, rows in standings.items():
            for s in rows:
                conn.execute("""
                    INSERT OR REPLACE INTO betpawa_standings
                        (round_id, league, league_id, position, team, played,
                         points, wins, draws, losses, gf, ga, gd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (str(round_id), league_name, s.get('league_id'),
                      s.get('pos'), s.get('team'), s.get('played'),
                      s.get('points', s.get('pts')),
                      s.get('w'), s.get('d'), s.get('l'),
                      s.get('gf'), s.get('ga'), s.get('gd')))

    conn.commit()
    conn.close()
    return cb


# ── BangBet writer ──────────────────────────────────────────────

def save_bangbet_round(schedule_time, tournament, tournament_id=None, timestamp=None,
                       matches=None, standings=None):
    """Save a bangbet round: round + matches + standings."""
    conn = _get_conn()
    cb = 1 if _chain_break('bangbet') else 0

    conn.execute("""
        INSERT OR REPLACE INTO bangbet_rounds
            (schedule_time, tournament, tournament_id, timestamp, collected_at,
             match_count, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (schedule_time, tournament, tournament_id, timestamp, _now(),
          len(matches) if matches else 0, SESSION_ID, cb))

    if matches:
        for i, m in enumerate(matches, 1):
            hg, ag = m.get('hg'), m.get('ag')
            outcome = 'W' if hg > ag else ('D' if hg == ag else 'L') if hg is not None else None
            parity = None if hg is None or ag is None else ('E' if (hg + ag) % 2 == 0 else 'O')
            periods = m.get('periods', [])
            ht_home = periods[0].get('homeScore') if len(periods) > 0 else None
            ht_away = periods[0].get('awayScore') if len(periods) > 0 else None

            conn.execute("""
                INSERT OR REPLACE INTO bangbet_matches
                    (schedule_time, tournament, match_n, home_team, away_team,
                     hg, ag, result, outcome, parity, ht_home, ht_away)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (schedule_time, tournament, i,
                  m.get('home'), m.get('away'),
                  hg, ag, m.get('score'),
                  outcome, parity, ht_home, ht_away))

    if standings:
        for s in standings:
            conn.execute("""
                INSERT OR REPLACE INTO bangbet_standings
                    (schedule_time, tournament, position, team, played,
                     points, wins, draws, losses, gf, ga, gd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (schedule_time, tournament, s.get('pos'), s.get('team'),
                  s.get('played'), s.get('points', s.get('pts')),
                  s.get('w'), s.get('d'), s.get('l'),
                  s.get('gf'), s.get('ga'), s.get('gd')))

    conn.commit()
    conn.close()
    return cb


# ── 22Bet writer ────────────────────────────────────────────────

def save_bet22_round(round_id, league=None, matches=None):
    """Save a 22bet round: round + match odds."""
    conn = _get_conn()
    cb = 1 if _chain_break('bet22') else 0

    conn.execute("""
        INSERT OR REPLACE INTO bet22_rounds
            (round_id, league, collected_at, match_count, session_id, chain_break)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (str(round_id), league, _now(),
          len(matches) if matches else 0, SESSION_ID, cb))

    if matches:
        for i, m in enumerate(matches, 1):
            od = m.get('odds', {})
            conn.execute("""
                INSERT OR REPLACE INTO bet22_matches
                    (round_id, match_id, match_n, home_team, away_team,
                     odd_1, odd_X, odd_2,
                     dc_1x, dc_x2, dc_12,
                     btts_yes, btts_no,
                     ou_data, handicap_data, ind_total_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(round_id), m.get('match_id'), i,
                  m.get('home_team'), m.get('away_team'),
                  od.get('1x2', {}).get('1'),
                  od.get('1x2', {}).get('X'),
                  od.get('1x2', {}).get('2'),
                  od.get('dc', {}).get('1x'),
                  od.get('dc', {}).get('x2'),
                  od.get('dc', {}).get('12'),
                  od.get('btts', {}).get('yes'),
                  od.get('btts', {}).get('no'),
                  json.dumps(od.get('ou', [])),
                  json.dumps(od.get('handicap', [])),
                  json.dumps(od.get('ind_total', {}))))

    conn.commit()
    conn.close()
    return cb


# ── Utility ─────────────────────────────────────────────────────

def get_session_info():
    """Return current session ID and which sources have written."""
    return {
        'session_id': SESSION_ID,
        'sources_written': [s for s, w in _has_written.items() if w],
    }


def init_db():
    """Ensure the schema exists. Call once at startup."""
    from schema import init_db as _init
    _init()
