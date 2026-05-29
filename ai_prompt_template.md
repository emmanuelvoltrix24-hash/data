# VFL Database — AI Prompt Template
## For OpenRouter / GPT-4 / Claude / Gemini

### Overview
You have read-only access to a PostgreSQL database tracking virtual football (VFL) matches across multiple betting sources. The data includes match results, pre-match odds (24 markets), standings, and mined prediction rules.

### Connection String
```
postgresql://postgres:YOGHRPNfBdouDkmpOfyRXeESXTjZkcXY@kodama.proxy.rlwy.net:30259/railway
```

### Tables

#### 1. `rounds` — Core match data (one row per round)
| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL | Auto-increment ID |
| `round_id` | TEXT | Unique round identifier (e.g. "9917640") |
| `source` | TEXT | Data source: `betkraft`, `bongobongo`, `betpawa`, `bangbet`, `22bet` |
| `league` | TEXT | League name (e.g. "English", "English League") |
| `collected_at` | TIMESTAMP | When this round was saved |
| `has_odds` | BOOLEAN | Whether pre-match odds exist |
| `has_standings` | BOOLEAN | Whether standings data exists |
| `has_ht` | BOOLEAN | Whether half-time scores exist |
| `data` | JSONB | Full round data (matches, standings, etc.) |

**The `data` JSONB structure:**
```json
{
  "league": "English",
  "source": "betkraft",
  "round_id": "9917640",
  "season_id": 9917495,
  "competition_id": 1,
  "chain_break": false,
  "collected_at": "2026-05-29 06:00:17",
  "matches": [
    {
      "n": 1,                           // Match slot (1-10)
      "home_team": "Bournemouth",
      "away_team": "N. Forest",
      "hg": 0,                          // Home goals
      "ag": 1,                          // Away goals
      "ht": "0:0",                      // Half-time score
      "result": "0:1",
      "outcome": "L",                   // W=HomeWin, D=Draw, L=AwayWin
      "parity": "O",                    // E=Even total goals, O=Odd
      "event_id": 9917640,
      "home_score_times": null,         // Minute of each home goal
      "away_score_times": [88],         // Minute of each away goal
      "pre_markets": {                  // 24 markets available
        "1X2": [{"outcome_id":"1","odd_value":"2.66"}, {"outcome_id":"X","odd_value":"3.37"}, {"outcome_id":"2","odd_value":"2.41"}],
        "GG": [{"outcome_id":"Y","odd_value":"1.68"}, {"outcome_id":"N","odd_value":"2.14"}],
        "TG25": [{"outcome_name":"Over 2.5","odd_value":"1.88"}, {"outcome_name":"Under 2.5","odd_value":"1.88"}],
        "TGOE": [{"outcome_id":"Even","odd_value":"1.79"}, {"outcome_id":"Odd","odd_value":"1.89"}],
        "DC": [{"outcome_id":"1X","odd_value":"1.49"}, {"outcome_id":"12","odd_value":"1.27"}, {"outcome_id":"X2","odd_value":"1.41"}],
        "CS": [{"outcome_id":"0-0","odd_value":"13.00"}, {"outcome_id":"1-0","odd_value":"9.00"}, ...],  // Correct Score
        "HS": [{"outcome_id":"0-0","odd_value":"3.94"}, ...],  // HT Score
        "MG": [{"outcome_id":"0-2","odd_value":"1.76"}, ...],  // Multi-Goals
        "TG": [{"outcome_id":"0","odd_value":"13.00"}, ...],   // Total Goals exact
        "TG15": [{"outcome_id":"O","odd_value":"1.28"}, ...],  // O/U 1.5
        "TG35": [{"outcome_id":"O","odd_value":"3.24"}, ...],  // O/U 3.5
        "FTS": [{"outcome_id":"H","odd_value":"1.93"}, ...],   // First Team to Score
        "HGG": [{"outcome_id":"Y","odd_value":"3.77"}, ...],   // HT Both Score
        "T1G": [{"outcome_id":"Y","odd_value":"1.27"}, ...],   // Team 1 Goal
        "T2G": [{"outcome_id":"Y","odd_value":"1.25"}, ...],   // Team 2 Goal
        "TFG": [{"outcome_id":"1-15","odd_value":"2.02"}, ...],// Time First Goal
        "1X2G": [{"outcome_id":"1G","odd_value":"5.38"}, ...], // 1X2+BTTS
        "DR": [{"outcome_id":"HH","odd_value":"4.11"}, ...],   // HT/FT Double Result
        "DCH": [{"outcome_id":"1X","odd_value":"1.36"}, ...],  // HT Double Chance
        "H1X2": [{"outcome_id":"1","odd_value":"3.04"}, ...],  // HT 1X2
        "T1OU15": [{"outcome_id":"O","odd_value":"2.50"}, ...],// Team1 O/U 1.5
        "T2OU15": [{"outcome_id":"O","odd_value":"2.31"}, ...],// Team2 O/U 1.5
        "1X2OU15": [{"outcome_id":"1O","odd_value":"3.63"}, ...],
        "1X2OU25": [{"outcome_id":"1O","odd_value":"4.74"}, ...]
      }
    }
    // ... 9 more matches (n=2 through n=10)
  ],
  "standings": [
    {
      "position": 1,
      "team": "CHE",
      "team_name": "London Blues",
      "points": 84,
      "team_form": "WWWWWL",   // last 6 results: W=win, D=draw, L=loss
      "season_id": 9917495
    }
    // ... 19 more teams
  ]
}
```

#### 2. `rules` — Mined prediction rules
| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL | Auto-increment |
| `target` | TEXT | What's being predicted. Format: `{slot}_{feature}={value}` e.g. "M5_outcome=W", "M10_parity=O" |
| `conditions` | JSONB | Trigger conditions. Format: `{"M3_parity":"E","M6_outcome":"L"}` |
| `lag` | INT | Round distance: 1=N→N+1, 2=N→N+2, 3=N→N+3 |
| `hits` | INT | Times conditions led to target |
| `total` | INT | Times conditions occurred |
| `precision` | FLOAT | hits/total = accuracy |
| `recall` | FLOAT | What fraction of all target occurrences this covers |
| `ev` | FLOAT | Expected value = precision × hits |
| `status` | TEXT | `active` (reliable) or `tentative` (< 8 samples) |
| `lift` | FLOAT | How much better than random |
| `pvalue` | FLOAT | Statistical significance |
| `stable` | BOOLEAN | Precision consistent across first/second half of data |
| `ensemble` | INT | How many independent rules predict the same target |
| `chained_via` | TEXT | If this is a chained rule A→B→C, shows the link |
| `discovered_at` | TIMESTAMP | When the rule was first found |
| `rounds_used` | INT | How many rounds were analyzed |

#### 3. `odds_comparison` — Flattened 1X2 odds per match
| Column | Description |
|--------|-------------|
| `round_id` | Round identifier |
| `match_n` | Match slot (1-10) |
| `home_team`, `away_team` | Team names |
| `source` | Data source |
| `h_odd`, `d_odd`, `a_odd` | Decimal odds for Home/Draw/Away |

#### 4. `failed_rules` — Rules that degraded over time
Rules that initially had high precision but failed on newer data.

### Available Views

#### `v_ai_features` — Denormalized match-level view
One row per match, columns: round_id, source, match_n, home_team, away_team, home_goals, away_goals, outcome, parity, ht_score, home_position, away_position, home_points, away_points, home_form, away_form, odd_h, odd_d, odd_a, gg_yes_odd, tg25_over_odd, ... plus all market odds as direct columns.

### Common Query Patterns

**Get matches for a specific slot across all rounds (e.g. M5):**
```sql
SELECT * FROM v_ai_features WHERE match_n = 5 ORDER BY round_id;
```

**Compare two slots in the same round (self-join):**
```sql
SELECT a.round_id, 
       a.outcome as m5_outcome, a.parity as m5_parity,
       b.outcome as m10_outcome, b.parity as m10_parity
FROM v_ai_features a 
JOIN v_ai_features b ON a.round_id = b.round_id AND a.source = b.source
WHERE a.match_n = 5 AND b.match_n = 10;
```

**Find high-precision rules:**
```sql
SELECT target, precision, lag, hits, total, conditions
FROM rules WHERE status='active' AND precision > 0.85
ORDER BY ev DESC LIMIT 20;
```

**Get market odds for any match:**
```sql
SELECT round_id, match_n, home_team, away_team, odd_h, odd_d, odd_a,
       ov25_odd, gg_yes_odd, even_odd
FROM v_ai_features
WHERE round_id = '9917640'
ORDER BY match_n;
```

### Task Instructions for the AI
You are a football prediction analyst. Your job is to:
1. Query the database to find statistical patterns across **all 10 match slots** (M1 through M10)
2. Generate prediction rules (conditions → outcome/parity for any slot)
3. Validate rules by cross-checking against historical data
4. Output rules in the format used by the `rules` table

**IMPORTANT: DO NOT focus only on M10.** Every slot (M1-M10) can be both a source of conditions AND a prediction target. Mine rules for ALL slots.

When mining rules, look for:
- **Cross-slot patterns:** Does M3's outcome predict M8's parity in the same round? Does M6's parity predict M2's outcome in the NEXT round?
- **Same-slot across rounds:** Does M5_outcome in round N predict M5_outcome in round N+1?
- **Round-level features:** Does the number of draws in round N predict any slot's outcome in round N+1?
- **Multi-slot sequence patterns:** Does the combined parity of M5+M6+M7 (e.g. "EEE", "OOO", "EOE") predict anything in round N+1? Does the M1+M2+M3 outcome pattern repeat or predict M8+M9+M10?
- **Full-round sequence repetition:** Does any 10-match outcome or parity sequence ever repeat? If so, what follows it?
- **Half-time ↔ Full-time:** Does HT parity/outcome predict FT outcome for the same match?
- **Standings:** Position diff, points diff, form streaks predicting match outcomes
- **Odds-implied probability vs actual results** (value betting / overround analysis)
- **Goal timing:** Early vs late goal patterns
- **Chain breaks:** Does a season reset change prediction dynamics?
- **Market consensus:** When multiple markets agree (1X2 + GG + O/U all point same direction), does precision improve?

**Multi-slot sequence columns available in `v_ai_features`:**
Use self-joins. Example — does M5M6M7 parity predict M10 outcome?
```sql
WITH round_seq AS (
  SELECT round_id,
         string_agg(outcome ORDER BY match_n) as m5m6m7_outcome,
         string_agg(parity ORDER BY match_n) as m5m6m7_parity
  FROM v_ai_features WHERE match_n IN (5,6,7)
  GROUP BY round_id
)
SELECT a.m5m6m7_parity, a.m5m6m7_outcome, b.match_n, b.outcome, b.parity
FROM round_seq a
JOIN v_ai_features b ON a.round_id = b.round_id
WHERE b.match_n = 10
ORDER BY a.round_id;
```
