#!/usr/bin/env python3
"""
BangBet Virtual Football Collector - Timer-Sync Edition
1. Fetches active tournaments.
2. Fetches match schedule (MatchDayList) for each.
3. Polls results (FinishedList) exactly when rounds conclude.
"""
import requests
import json
import time
import os
import logging
import random
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# Endpoints
BASE_URL = "https://bet-api.bangbet.com/api/bet"
TOURNAMENT_URL = f"{BASE_URL}/virtualArea/tournamentList?country=ug&producer=6&sportId=sr:sport:1"
MATCHDAY_URL = f"{BASE_URL}/virtual/match/matchDayList"
RESULTS_URL = f"{BASE_URL}/virtual/match/finished/list"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

SAVE_DIR = "/home/voltrix/bangbet_vfl_data"
os.makedirs(SAVE_DIR, exist_ok=True)

class BangBetSession:
    def __init__(self):
        self.session = requests.Session()
        self.update_headers()

    def update_headers(self):
        self.session.headers.update({
            "user-agent": random.choice(USER_AGENTS),
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "referer": "https://www.bangbet.com/virtuals/",
            "origin": "https://www.bangbet.com"
        })

    def request(self, method, url, payload=None):
        try:
            time.sleep(random.uniform(0.5, 1.5))
            if method == "POST":
                r = self.session.post(url, json=payload, timeout=15)
            else:
                r = self.session.get(url, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Request failed (%s): %s", url, e)
            return None

def _empty_row():
    return {"played": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "gd": 0, "pts": 0}


def compute_standings(tournament_name):
    table = {}
    for fn in sorted(os.listdir(SAVE_DIR)):
        if not fn.endswith(".json") or fn == "latest_standings.json":
            continue
        try:
            with open(os.path.join(SAVE_DIR, fn)) as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("tournament") != tournament_name:
            continue
        for m in d.get("matches", []):
            score = m.get("score", "")
            if ":" not in score:
                continue
            hg, ag = int(score.split(":")[0]), int(score.split(":")[1])
            for team in (m["home"], m["away"]):
                table.setdefault(team, _empty_row())
            h, a = table[m["home"]], table[m["away"]]
            h["played"] += 1; a["played"] += 1
            h["gf"] += hg; h["ga"] += ag
            a["gf"] += ag; a["ga"] += hg
            if hg > ag:
                h["w"] += 1; h["pts"] += 3; a["l"] += 1
            elif hg < ag:
                a["w"] += 1; a["pts"] += 3; h["l"] += 1
            else:
                h["d"] += 1; a["d"] += 1; h["pts"] += 1; a["pts"] += 1
    for row in table.values():
        row["gd"] = row["gf"] - row["ga"]
    ranked = sorted(table.items(), key=lambda x: (-x[1]["pts"], -x[1]["gd"], -x[1]["gf"]))
    return [{"pos": i + 1, "team": t, **r} for i, (t, r) in enumerate(ranked)]


def save_standings(tournament_name):
    rows = compute_standings(tournament_name)
    if not rows:
        return
    with open(f"{SAVE_DIR}/latest_standings.json", "w") as f:
        json.dump(rows, f, indent=2)


def display_standings(tournament_name):
    rows = compute_standings(tournament_name)
    if not rows:
        return
    print(f"\n  STANDINGS — {tournament_name}")
    print(f"  {'#':<4} {'Team':<25} {'P':>3} {'W':>3} {'D':>3} {'L':>3} {'GF':>4} {'GA':>4} {'GD':>4} {'Pts':>4}")
    print(f"  {'-' * 65}")
    for r in rows:
        print(f"  {r['pos']:<4} {r['team']:<25} {r['played']:>3} {r['w']:>3} {r['d']:>3} {r['l']:>3} {r['gf']:>4} {r['ga']:>4} {r['gd']:>4} {r['pts']:>4}")
    print()


def display_results(data):
    ts = data.get("timestamp", "Unknown")
    tournament = data.get("tournament", "Unknown")
    log.info("BANGBET | %s | %s", tournament, ts)
    header = f"  {'Home':<25} | {'Away':<25} | {'Score':^10} | {'HT':^7}"
    print("\n" + header + "\n  " + "-" * len(header))
    for m in data["matches"]:
        ht = m["periods"][0] if m.get("periods") else {}
        ht_score = f"{ht.get('homeScore')}:{ht.get('awayScore')}" if ht else "-:-"
        print(f"  {m['home']:<25} | {m['away']:<25} | {m['score']:^10} | {ht_score:^7}")
    print("  " + "=" * len(header) + "\n")

def monitor():
    api = BangBetSession()
    processed_keys = set()
    log.info("BangBet Timer-Sync Collector started.")

    while True:
        try:
            # 1. Get Tournament List
            t_data = api.request("GET", TOURNAMENT_URL)
            if not t_data or not t_data.get("data"):
                time.sleep(30); continue
            
            tournaments = t_data["data"]
            
            for t in tournaments:
                t_id = t["tournamentId"]
                t_name = t["tournamentName"]
                
                # 2. Get Match Day List (The Timer)
                md_payload = {"producer": 6, "tournamentId": t_id, "country": "ug"}
                md_data = api.request("POST", MATCHDAY_URL, md_payload)
                if not md_data or not md_data.get("data"): continue
                
                standings_dirty = False

                # 3. Process each round in the schedule
                for round_info in md_data["data"]:
                    st = round_info["scheduleDate"]
                    key = f"{t_id}_{st}"
                    
                    if key in processed_keys: continue
                    
                    if round_info["status"] == 2:
                        res_payload = {
                            "country": "ug",
                            "tournamentId": t_id,
                            "producer": 6,
                            "sportId": "sr:sport:1",
                            "betradarId": round_info["betradarId"],
                            "number": round_info["number"],
                            "seasonId": round_info["seasonId"],
                            "scheduleDate": st
                        }
                        
                        res_data = api.request("POST", RESULTS_URL, res_payload)
                        if res_data and res_data.get("data"):
                            matches = []
                            for m in res_data["data"]:
                                matches.append({
                                    "home": m["homeTeamName"],
                                    "away": m["awayTeamName"],
                                    "score": f"{m['homeScore']}:{m['awayScore']}",
                                    "periods": m.get("periodScoreList", [])
                                })
                            
                            round_result = {
                                "tournament": t_name,
                                "tournamentId": t_id,
                                "scheduleTime": st,
                                "timestamp": datetime.fromtimestamp(st/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                                "matches": matches
                            }
                            
                            display_results(round_result)

                            fn = f"{SAVE_DIR}/{t_name.replace(' ', '_')}_{st}.json"
                            with open(fn, "w") as f: json.dump(round_result, f, indent=2)

                            # Write to unified DB
                            try:
                                from db import save_round as db_save_round
                                db_matches = [{'n': i+1, 'home': m['home'], 'away': m['away'],
                                               'hg': int(m['score'].split(':')[0]),
                                               'ag': int(m['score'].split(':')[1]),
                                               'result': m['score']} for i, m in enumerate(matches)]
                                db_save_round(str(st), 'bangbet', t_name, db_matches)
                            except Exception as e:
                                log.warning("DB write error: %s", e)

                            processed_keys.add(key)
                            standings_dirty = True

                if standings_dirty:
                    save_standings(t_name)
                    display_standings(t_name)

            # Cleanup processed_keys to prevent memory leak
            if len(processed_keys) > 5000: processed_keys.clear()

        except Exception as e:
            log.error("Monitor loop error: %s", e)
        
        # Wait a bit before next schedule check
        time.sleep(60)

if __name__ == "__main__":
    monitor()
