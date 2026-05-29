-- VFL AI View — Flattened match-level data for AI consumption
-- Run: psql DATABASE_URL -f ai_view.sql
-- Each row = one match from one round, with all fields as direct columns

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
    m.value->>'home_team' as home_team,
    m.value->>'away_team' as away_team,
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

  -- Goal timing
  jsonb_array_length(md.home_goal_times) as home_goal_count,
  jsonb_array_length(md.away_goal_times) as away_goal_count,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.home_goal_times) t WHERE t::int > 60) as home_late_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.away_goal_times) t WHERE t::int > 60) as away_late_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.home_goal_times) t WHERE t::int <= 30) as home_early_goals,
  (SELECT COUNT(*) FROM jsonb_array_elements_text(md.away_goal_times) t WHERE t::int <= 30) as away_early_goals,

  -- Standings for home team
  (SELECT (s.value->>'position')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.home_team LIMIT 1) as home_position,
  (SELECT (s.value->>'points')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.home_team LIMIT 1) as home_points,
  (SELECT s.value->>'team_form' FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.home_team LIMIT 1) as home_form,

  -- Standings for away team
  (SELECT (s.value->>'position')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.away_team LIMIT 1) as away_position,
  (SELECT (s.value->>'points')::int FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.away_team LIMIT 1) as away_points,
  (SELECT s.value->>'team_form' FROM jsonb_array_elements(md.standings_json) s 
   WHERE s.value->>'team_name' = md.away_team LIMIT 1) as away_form,

  -- 1X2 odds
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o 
   WHERE o.value->>'outcome_id' = '1' LIMIT 1)::float as odd_h,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o 
   WHERE o.value->>'outcome_id' = 'X' LIMIT 1)::float as odd_d,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2') o 
   WHERE o.value->>'outcome_id' = '2' LIMIT 1)::float as odd_a,

  -- GG/BTTS odds
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'GG') o 
   WHERE o.value->>'outcome_id' IN ('Y','Yes') LIMIT 1)::float as gg_yes_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'GG') o 
   WHERE o.value->>'outcome_id' IN ('N','No') LIMIT 1)::float as gg_no_odd,

  -- O/U 2.5 odds
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TG25') o 
   WHERE o.value->>'outcome_name' ILIKE '%over%' LIMIT 1)::float as ov25_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TG25') o 
   WHERE o.value->>'outcome_name' ILIKE '%under%' LIMIT 1)::float as un25_odd,

  -- O/U 1.5
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TG15') o 
   WHERE o.value->>'outcome_id' = 'O' LIMIT 1)::float as ov15_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TG35') o 
   WHERE o.value->>'outcome_id' = 'O' LIMIT 1)::float as ov35_odd,

  -- Total Goals odd/even
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TGOE') o 
   WHERE o.value->>'outcome_id' = 'E' LIMIT 1)::float as even_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TGOE') o 
   WHERE o.value->>'outcome_id' = 'O' LIMIT 1)::float as odd_goals_odd,

  -- Double Chance
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DC') o 
   WHERE o.value->>'outcome_id' = '1X' LIMIT 1)::float as dc_1x_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DC') o 
   WHERE o.value->>'outcome_id' = '12' LIMIT 1)::float as dc_12_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DC') o 
   WHERE o.value->>'outcome_id' = 'X2' LIMIT 1)::float as dc_x2_odd,

  -- First Team to Score
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'FTS') o 
   WHERE o.value->>'outcome_id' = 'H' LIMIT 1)::float as fts_h_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'FTS') o 
   WHERE o.value->>'outcome_id' = 'A' LIMIT 1)::float as fts_a_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'FTS') o 
   WHERE o.value->>'outcome_id' = '0' LIMIT 1)::float as fts_0_odd,

  -- HT 1X2
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'H1X2') o 
   WHERE o.value->>'outcome_id' = '1' LIMIT 1)::float as ht_1x2_h_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'H1X2') o 
   WHERE o.value->>'outcome_id' = 'X' LIMIT 1)::float as ht_1x2_d_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'H1X2') o 
   WHERE o.value->>'outcome_id' = '2' LIMIT 1)::float as ht_1x2_a_odd,

  -- Team Goal/No Goal
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'T1G') o 
   WHERE o.value->>'outcome_id' = 'Y' LIMIT 1)::float as t1g_yes_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'T1G') o 
   WHERE o.value->>'outcome_id' = 'N' LIMIT 1)::float as t1g_no_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'T2G') o 
   WHERE o.value->>'outcome_id' = 'Y' LIMIT 1)::float as t2g_yes_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'T2G') o 
   WHERE o.value->>'outcome_id' = 'N' LIMIT 1)::float as t2g_no_odd,

  -- HT Both Score
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'HGG') o 
   WHERE o.value->>'outcome_id' = 'Y' LIMIT 1)::float as hgg_yes_odd,

  -- Time of First Goal
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TFG') o 
   WHERE o.value->>'outcome_id' = '1' LIMIT 1)::float as tfg_1_15_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'TFG') o 
   WHERE o.value->>'outcome_id' = '0' LIMIT 1)::float as tfg_no_goal_odd,

  -- Multi-Goals
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'MG') o 
   WHERE o.value->>'outcome_id' = '0-2' LIMIT 1)::float as mg_02_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'MG') o 
   WHERE o.value->>'outcome_id' = '1-3' LIMIT 1)::float as mg_13_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'MG') o 
   WHERE o.value->>'outcome_id' = '2-4' LIMIT 1)::float as mg_24_odd,

  -- Correct Score (most common)
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '1-0' LIMIT 1)::float as cs_10_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '2-1' LIMIT 1)::float as cs_21_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '0-0' LIMIT 1)::float as cs_00_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '1-1' LIMIT 1)::float as cs_11_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '0-1' LIMIT 1)::float as cs_01_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'CS') o 
   WHERE o.value->>'outcome_id' = '2-0' LIMIT 1)::float as cs_20_odd,

  -- 1X2 & BTTS combo
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2G') o 
   WHERE o.value->>'outcome_id' = '1G' LIMIT 1)::float as x2g_1g_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'1X2G') o 
   WHERE o.value->>'outcome_id' = '2G' LIMIT 1)::float as x2g_2g_odd,

  -- HT/FT Double Result (common: HH, DD, AA)
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DR') o 
   WHERE o.value->>'outcome_id' = 'HH' LIMIT 1)::float as dr_hh_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DR') o 
   WHERE o.value->>'outcome_id' = 'DD' LIMIT 1)::float as dr_dd_odd,
  (SELECT o.value->>'odd_value' FROM jsonb_array_elements(md.pre_markets->'DR') o 
   WHERE o.value->>'outcome_id' = 'AA' LIMIT 1)::float as dr_aa_odd

FROM match_data md
ORDER BY md.round_id, md.match_n;

-- Grant access
GRANT SELECT ON v_ai_features TO PUBLIC;
