-- VFL AI View — Flattened match-level data for AI consumption
-- Handles all 4 source formats: betkraft (home_team/pre_markets),
-- bangbet/betpawa/bongobongo (home/away/odds.1x2)
-- Run: psql DATABASE_URL -f ai_view.sql

CREATE OR REPLACE VIEW v_ai_features AS
WITH match_data AS (
  SELECT 
    r.id as round_pk,
    r.round_id,
    r.source,
    r.league,
    r.collected_at,
    m.value as match_json,
    (m.value->>'n')::int as match_n,
    COALESCE(m.value->>'home_team', m.value->>'home') as home_team,
    COALESCE(m.value->>'away_team', m.value->>'away') as away_team,
    (m.value->>'hg')::int as home_goals,
    (m.value->>'ag')::int as away_goals,
    m.value->>'outcome' as outcome,
    m.value->>'parity' as parity,
    m.value->>'ht' as ht_score,
    m.value->>'result' as result,
    m.value->>'event_id' as event_id,
    (m.value->>'home_score_times')::jsonb as home_goal_times,
    (m.value->>'away_score_times')::jsonb as away_goal_times,
    m.value->'pre_markets' as pre_markets,
    -- odds from 'odds.1x2' format (bangbet/betpawa/bongobongo)
    m.value->'odds'->'1x2'->>'1' as odd_h_flat,
    m.value->'odds'->'1x2'->>'X' as odd_d_flat,
    m.value->'odds'->'1x2'->>'2' as odd_a_flat,
    r.data->'standings' as standings_json,
    r.data->>'season_id' as season_id,
    (r.data->>'chain_break')::boolean as chain_break
  FROM rounds r, jsonb_array_elements(r.data->'matches') m
)
SELECT 
  md.round_id,
  md.source,
  md.match_n,
  md.home_team,
  md.away_team,
  md.league,
  md.collected_at,
  md.home_goals,
  md.away_goals,
  md.home_goals + md.away_goals as total_goals,
  md.outcome,
  md.parity,
  md.ht_score,
  md.result,
  md.chain_break,

  -- HT features
  CASE WHEN md.ht_score ~ '^\d+:\d+$' THEN
    SPLIT_PART(md.ht_score, ':', 1)::int
  END as ht_home_goals,
  CASE WHEN md.ht_score ~ '^\d+:\d+$' THEN
    SPLIT_PART(md.ht_score, ':', 2)::int
  END as ht_away_goals,

  -- Goal timing (betkraft only)
  jsonb_array_length(md.home_goal_times) as home_goal_count,
  jsonb_array_length(md.away_goal_times) as away_goal_count,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.home_goal_times) t WHERE t::int > 60) as home_late_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.away_goal_times) t WHERE t::int > 60) as away_late_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.home_goal_times) t WHERE t::int <= 30) as home_early_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.away_goal_times) t WHERE t::int <= 30) as away_early_goals,

  -- Standings for home team
  (SELECT (s.value->>'position')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.home_team OR s.value->>'team_name' = md.home_team LIMIT 1) as home_position,
  (SELECT (s.value->>'points')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.home_team OR s.value->>'team_name' = md.home_team LIMIT 1) as home_points,
  (SELECT s.value->>'form' FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.home_team OR s.value->>'team_name' = md.home_team LIMIT 1) as home_form,

  -- Standings for away team
  (SELECT (s.value->>'position')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.away_team OR s.value->>'team_name' = md.away_team LIMIT 1) as away_position,
  (SELECT (s.value->>'points')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.away_team OR s.value->>'team_name' = md.away_team LIMIT 1) as away_points,
  (SELECT s.value->>'form' FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team' = md.away_team OR s.value->>'team_name' = md.away_team LIMIT 1) as away_form,

  -- 1X2 odds (flat format from odds.1x2 — bangbet/betpawa/bongobongo)
  md.odd_h_flat::float as odd_h_flat,
  md.odd_d_flat::float as odd_d_flat,
  md.odd_a_flat::float as odd_a_flat,
  -- 1X2 odds (market list format from pre_markets — betkraft)
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o LIMIT 1)::float as odd_h_mkt,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o OFFSET 1 LIMIT 1)::float as odd_d_mkt,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o OFFSET 2 LIMIT 1)::float as odd_a_mkt,

  -- GG/BTTS odds
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'GG') o LIMIT 1)::float as gg_yes_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'GG') o OFFSET 1 LIMIT 1)::float as gg_no_odd,

  -- Unified odds (prefer flat, fallback to market)
  COALESCE(md.odd_h_flat::float, 
    (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o LIMIT 1)::float) as odd_h,
  COALESCE(md.odd_d_flat::float,
    (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o OFFSET 1 LIMIT 1)::float) as odd_d,
  COALESCE(md.odd_a_flat::float,
    (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o OFFSET 2 LIMIT 1)::float) as odd_a

FROM match_data md
WHERE md.home_team IS NOT NULL AND md.away_team IS NOT NULL;

-- Sequence view: round N → N+1 comparison for cross-round patterns
CREATE OR REPLACE VIEW v_ai_sequences AS
SELECT 
  a.round_id as round_n,
  b.round_id as round_n1,
  a.source,
  a.match_n,
  a.home_team as home_team_n,
  a.away_team as away_team_n,
  a.outcome as outcome_n,
  a.parity as parity_n,
  a.total_goals as total_goals_n,
  b.outcome as outcome_n1,
  b.parity as parity_n1,
  b.total_goals as total_goals_n1,
  b.odd_h as odd_h_n1,
  b.odd_d as odd_d_n1,
  b.odd_a as odd_a_n1
FROM v_ai_features a
JOIN v_ai_features b ON a.source = b.source 
  AND a.match_n = b.match_n
  AND b.round_id > a.round_id
  AND NOT EXISTS (
    SELECT 1 FROM rounds r 
    WHERE r.source = a.source 
    AND r.round_id > a.round_id 
    AND r.round_id < b.round_id
  );
