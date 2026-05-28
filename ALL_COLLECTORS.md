# VFL Collectors — Complete Reference

## 1. Betkraft (Primary)
**File:** `railway_collector.py` / `local_collector.py`  
**API Base:** `https://vl.betkraft.co.uk`  
**PID:** `15740` | **Status:** ✅ Live

### Endpoints
| Endpoint | Method | Returns |
|---|---|---|
| `/periods/1` | GET | List of upcoming rounds with start/end times (UTC+3) |
| `/live` | POST | 10 match results during 37s live window |
| `/data/{round_id}` | POST | 27 market odds per match |
| `/standing/1/0` | GET | Full league table (20 teams) |
| `/results/1/0` | GET | History of completed rounds |

### Data Collected
- ✅ 10 matches per round
- ✅ Results (hg:ag, outcome, parity)
- ✅ 27 market odds (1X2, GG, TG15, TG25, DC, TG35, H1X2, DCH, HS, 1X2G, CS, DR, FTS, HGG, MG, T1G, T1OU15, T2G, T2OU15, TFG, TG, TGOE, 1X2OU15/25/35/45/55)
- ✅ Standings (20 teams, pos/points/form)
- ✅ Team form strings (htf/atf = last 6 results)

### Timing
- Rounds every ~2 minutes
- Trading window: 37 seconds
- Fire at kickoff via `/live` POST

---

## 2. BongoBongo
**File:** `bongobongo_collector.py`  
**API Base:** `https://vgp.sociumhubeurope.com/f1x2games/football1x2/latestresult.jsp`  
**PID:** `36889` | **Status:** ✅ Live

### Data
- Single XML request returns everything
- 10 matches per matchday
- 1X2 odds for upcoming matches
- Results for previous matchday
- Standings (20 teams with full stats)

### Team Mapping
| ID | Team |
|---|---|
| 1 | Manchester Blue |
| 2 | Manchester Red |
| 3 | Spurs |
| 4 | London Reds |
| 5 | London Blues |
| 6 | The Reds |
| 7 | Newcastle |
| 8 | WestHam |
| 9 | Brighton |
| 10 | Fulham |
| 11 | Brentford |
| 12 | N. Forest |
| 13 | Sunderland |
| 14 | Bournemouth |
| 15 | Burnley |
| 16 | Everton |
| 17 | Villa |
| 18 | Wolves |
| 19 | Leeds |
| 20 | Palace |

### Timing
- Polls every 2 seconds
- New matchday detected immediately

---

## 3. BetPawa
**File:** `betpawa_collector.py`  
**API Base:** `https://www.betpawa.ug/api/sportsbook/virtual`  
**PID:** `85331` | **Status:** ✅ Live

### Endpoints
| Endpoint | Method | Returns |
|---|---|---|
| `/v1/seasons/list/actual` | GET | Seasons with round schedules |
| `/v2/events/list/by-round/{id}?page=upcoming` | GET | 66 events with results + markets |

### Data Collected
- ✅ 66 events per round
- ✅ Results (FT scores)
- ✅ 5 market types:
  - 1X2 - FT (H/D/A odds)
  - Both Teams To Score - FT (Yes/No)
  - Double Chance - FT (1X/X2/12)
  - Total Score Over/Under - FT (3 lines: ~2.5, ~3.5, ~4.5)
  - HT / FT (9 combos: 1/1, 1/X, 1/2, X/1, X/X, X/2, 2/1, 2/X, 2/2)
- ✅ League info per event (7 leagues mixed per round)
- ✅ Standings computed from history per league

### League IDs
| ID | League | Teams |
|---|---|---|
| 7794 | English League | 20 |
| 7795 | Spanish League | 20 |
| 7796 | Italian League | 20 |
| 9183 | French League | 20 |
| 9184 | German League | 20 |
| 13773 | Portuguese League | 20 |
| 13774 | Dutch League | 20 |

### Timing
- Phase 1: Cache odds during 5min trading window
- Phase 2: Poll for results every 3s during simulation
- Results available ~1-2min after trading ends

---

## 4. BangBet
**File:** `bangbet_collector.py`  
**API Base:** `https://bet-api.bangbet.com/api/bet`  
**PID:** `139676` | **Status:** ✅ Live

### Endpoints
| Endpoint | Method | Returns |
|---|---|---|
| `/virtualArea/tournamentList` | GET | 8 tournaments |
| `/virtual/match/matchDayList` | POST | Match schedule by tournament |
| `/virtual/match/finished/list` | POST | Finished match results |
| `/virtual/match/list` | POST | Live matches with potential odds/markets |

### Data Collected
- ✅ Results with real team names
- ✅ HT (half-time) scores
- ✅ Period-by-period scoring
- ✅ Standings computed from history
- ✅ Market/odds data when available (via `/virtual/match/list`)

### Tournaments
| ID | Name | Matches/Round |
|---|---|---|
| 21 | English League | 8 |
| 22 | Bundesliga | 8 |
| 23 | League Mode | 8 |
| 25 | World Cup | 2 |
| 27 | Champions Cup | 2 |
| 29 | Asian Cup | 2 |
| 30 | Nations Cup | 2 |
| 31 | Euro Cup | 2 |

### Timing
- Polls schedule every 30s
- Captures finished rounds immediately

---

## 5. 22Bet
**File:** `bet22_collector.py`  
**API Base:** `https://22bet.ug/service-api/LineFeed/Get1x2_VZip`  
**PID:** `117642` | **Status:** ⏸️ Idle (waiting for events)

### Endpoints
| Endpoint | Returns |
|---|---|
| `Get1x2_VZip?champs=88637&count=50&lng=en_GB&mode=4&country=191` | Live events with full odds |

### Data Collected
- ✅ Real team names
- ✅ 1X2 odds
- ✅ Double Chance (1X/X2/12)
- ✅ BTTS (Yes/No)
- ✅ Over/Under (5 lines: 2.5, 3, 3.5, 4, 4.5)
- ✅ Handicap (12 variations)
- ✅ Individual Total (Home/Away)

### Timing
- Polls every 30s
- API returns `Value: []` when idle

---

## Output Directory
All collectors save to: **`/home/voltrix/vfl_data/`**

### File Naming
- `round_{id}_{timestamp}.json` — Betkraft
- `bongobongo_results_{id}_{timestamp}.json` — BongoBongo
- `betpawa_round_{id}_{timestamp}.json` — BetPawa
- `bangbet_{Tournament}_{schedule}.json` — BangBet
- `22bet_round_{id}_{timestamp}.json` — 22Bet

### Common Schema (per match)
```json
{
  "home_team": "...",
  "away_team": "...",
  "hg": int,
  "ag": int,
  "result": "hg:ag",
  "outcome": "W|D|L",
  "parity": "E|O",
  "odds": { ... },
  "league": "..."
}
```
