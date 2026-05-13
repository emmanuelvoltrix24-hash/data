#!/usr/bin/env python3
"""
BetPawa Virtual Football Collector
Polls BetPawa Virtuals API and outputs each new round as JSON with:
- round_id, week (name), season, timestamp
- matches: home/away team, odds (all markets), result (when available)
- standings: computed per-season per-league from saved results
"""
import requests
import json
import time
import os
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BASE_URL = "https://www.betpawa.ug/api/sportsbook/virtual"
SEASONS_URL = f"{BASE_URL}/v1/seasons/list/actual"
EVENTS_URL = f"{BASE_URL}/v2/events/list/by-round/{{round_id}}?page=upcoming"
POLL_INTERVAL = 30

HEADERS = {
    "x-pawa-brand": "betpawa-uganda",
    "x-pawa-language": "en",
    "devicetype": "web",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SAVE_DIR = "/home/voltrix/betpawa_vfl_data"
os.makedirs(SAVE_DIR, exist_ok=True)


def fetch_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("Request failed for %s: %s", url, e)
        return None


def get_actual_seasons():
    data = fetch_json(SEASONS_URL)
    return data.get("items", []) if data else []


def get_round_events(round_id):
    data = fetch_json(EVENTS_URL.format(round_id=round_id))
    return data.get("items", []) if data else []


def parse_result(results_obj):
    if not results_obj or "participantPeriodResults" not in results_obj:
        return None
    scores, ht = {}, {}
    for p_res in results_obj["participantPeriodResults"]:
        p_type = p_res["participant"]["type"]
        for period in p_res["periodResults"]:
            slug = period["period"]["slug"]
            if slug == "FULL_TIME_EXCLUDING_OVERTIME":
                scores[p_type] = int(period["result"])
            elif slug == "FIRST_HALF":
                ht[p_type] = int(period["result"])
    if "HOME" in scores and "AWAY" in scores:
        return {
            "home": scores["HOME"], "away": scores["AWAY"],
            "ht_home": ht.get("HOME"), "ht_away": ht.get("AWAY"),
        }
    return None


def parse_odds(event):
    odds = {}
    for market in event.get("markets", []):
        name = market["marketType"]["name"]
        rows = market.get("row", [])
        if name == "1X2 - FT":
            odds["1x2"] = {p["name"]: p["price"] for p in rows[0]["prices"]}
        elif name == "Both Teams To Score - FT":
            odds["btts"] = {p["name"]: p["price"] for p in rows[0]["prices"]}
        elif name == "Double Chance - FT":
            odds["dc"] = {p["name"]: p["price"] for p in rows[0]["prices"]}
        elif name == "Total Score Over/Under - FT":
            odds["ou"] = [{p["name"]: p["price"] for p in row["prices"]} for row in rows]
        elif name == "HT / FT":
            htft = {}
            for row in rows:
                for p in row["prices"]:
                    htft[p["name"]] = p["price"]
            odds["htft"] = htft
    return odds


def process_round(season, round_info):
    events = get_round_events(round_info["id"])
    if not events:
        return None

    groups = {}
    for event in events:
        league = event.get("region", {}).get("name", "Unknown")
        groups.setdefault(league, []).append(event)

    rounds = []
    for league_name, league_events in groups.items():
        matches = []
        for event in league_events:
            res = parse_result(event.get("results"))
            matches.append({
                "id": event["id"],
                "home": event["participants"][0]["name"],
                "away": event["participants"][1]["name"],
                "odds": parse_odds(event),
                "result": f"{res['home']}:{res['away']}" if res else None,
                "hg": res["home"] if res else None,
                "ag": res["away"] if res else None,
                "ht": f"{res['ht_home']}:{res['ht_away']}" if res and res.get("ht_home") is not None else None,
            })
        rounds.append({
            "round_id": round_info["id"],
            "round_name": round_info["name"],
            "season_id": season["id"],
            "league": league_name,
            "timestamp": round_info["tradingTime"]["start"],
            "matches": matches,
        })
    return rounds


# ── Standings ─────────────────────────────────────────────────────────────────

def _empty_row():
    return {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0}


def compute_standings(season_id, league):
    table = {}
    for fn in sorted(os.listdir(SAVE_DIR)):
        if not (fn.startswith("round_") and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(SAVE_DIR, fn)) as f:
                d = json.load(f)
        except Exception:
            continue
        if d["season_id"] != season_id or d["league"] != league:
            continue
        if not all(m["result"] for m in d["matches"]):
            continue
        for m in d["matches"]:
            hg, ag = m["hg"], m["ag"]
            if hg is None:
                continue
            for team in (m["home"], m["away"]):
                table.setdefault(team, _empty_row())
            h, a = table[m["home"]], table[m["away"]]
            h["P"] += 1; a["P"] += 1
            h["GF"] += hg; h["GA"] += ag
            a["GF"] += ag; a["GA"] += hg
            if hg > ag:
                h["W"] += 1; h["Pts"] += 3; a["L"] += 1
            elif hg < ag:
                a["W"] += 1; a["Pts"] += 3; h["L"] += 1
            else:
                h["D"] += 1; a["D"] += 1; h["Pts"] += 1; a["Pts"] += 1
    for row in table.values():
        row["GD"] = row["GF"] - row["GA"]
    return sorted(table.items(), key=lambda x: (-x[1]["Pts"], -x[1]["GD"], -x[1]["GF"]))


def display_standings(season_id, league):
    rows = compute_standings(season_id, league)
    if not rows:
        return
    print(f"\n  STANDINGS — {league} (Season {season_id})")
    print(f"  {'#':<4} {'Team':<15} {'P':>3} {'W':>3} {'D':>3} {'L':>3} {'GF':>4} {'GA':>4} {'GD':>4} {'Pts':>4}")
    print(f"  {'-' * 60}")
    for i, (team, r) in enumerate(rows, 1):
        print(f"  {i:<4} {team:<15} {r['P']:>3} {r['W']:>3} {r['D']:>3} {r['L']:>3} {r['GF']:>4} {r['GA']:>4} {r['GD']:>4} {r['Pts']:>4}")
    print()


def save_standings(season_id, league):
    rows = compute_standings(season_id, league)
    if not rows:
        return
    safe_league = league.replace(" ", "_").replace("/", "-")
    filename = f"{SAVE_DIR}/standings_{season_id}_{safe_league}.json"
    with open(filename, "w") as f:
        json.dump([{"team": t, **r} for t, r in rows], f, indent=2)


# ── Display / Save ────────────────────────────────────────────────────────────

def display_round(data):
    has_results = all(m["result"] for m in data["matches"])
    status = "RESULTS" if has_results else "UPCOMING/ODDS"
    log.info("%s | League: %s | Season: %s | Round: %s (ID: %s)",
             status, data["league"], data["season_id"], data["round_name"], data["round_id"])
    print(f"  {'#':<4} {'Home':<15} {'Away':<15} {'H':>7} {'D':>7} {'A':>7} | Result  | HT")
    print(f"  {'-' * 80}")
    for i, m in enumerate(data["matches"], 1):
        x2 = m["odds"].get("1x2", {})
        h, d, a = x2.get("1", "-"), x2.get("X", "-"), x2.get("2", "-")
        res = m["result"] or "PENDING"
        ht = m.get("ht") or "-"
        print(f"  {i:<4} {m['home']:<15} {m['away']:<15} {str(h):>7} {str(d):>7} {str(a):>7} | {res:<7}  | {ht}")

    # Extra markets sample (first match)
    m0 = data["matches"][0]
    if m0["odds"].get("btts"):
        b = m0["odds"]["btts"]
        print(f"\n  [Match 1 extra odds] BTTS Yes={b.get('Yes')} No={b.get('No')}", end="")
    if m0["odds"].get("dc"):
        dc = m0["odds"]["dc"]
        print(f"  |  DC 1X={dc.get('1X')} X2={dc.get('X2')} 12={dc.get('12')}", end="")
    if m0["odds"].get("ou"):
        for i, line in enumerate(m0["odds"]["ou"]):
            print(f"\n  O/U line {i+1}: Over={line.get('Over')} Under={line.get('Under')}", end="")
    if m0["odds"].get("btts") or m0["odds"].get("ou"):
        print()
    print("-" * 80 + "\n")


def save_round(data):
    safe_league = data["league"].replace(" ", "_").replace("/", "-")
    filename = f"{SAVE_DIR}/round_{data['round_id']}_{safe_league}.json"
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    # Write to unified DB
    try:
        from db import save_round as db_save_round
        db_matches = [{'n': i+1, 'home': m['home'], 'away': m['away'],
                       'hg': m.get('hg') or 0, 'ag': m.get('ag') or 0,
                       'result': m.get('result') or '',
                       'odds': {'1x2': m.get('odds', {}).get('1x2', {})},
                       'ht': m.get('ht')}
                      for i, m in enumerate(data['matches'])]
        db_save_round(str(data['round_id']), 'betpawa', data.get('league',''), db_matches)
    except Exception as e:
        log.warning("DB write error: %s", e)
    return filename


# ── Monitor ───────────────────────────────────────────────────────────────────

def monitor():
    processed_finished = set()
    processed_upcoming = set()

    for fn in os.listdir(SAVE_DIR):
        if fn.startswith("round_") and fn.endswith(".json"):
            try:
                with open(os.path.join(SAVE_DIR, fn)) as f:
                    d = json.load(f)
                key = (d["round_id"], d["league"])
                if all(m["result"] for m in d["matches"]):
                    processed_finished.add(key)
                else:
                    processed_upcoming.add(key)
            except Exception as e:
                log.warning("Could not load %s: %s", fn, e)

    log.info("BetPawa VFL Collector started. Loaded %d finished, %d upcoming rounds.",
             len(processed_finished), len(processed_upcoming))

    while True:
        try:
            now = datetime.now(timezone.utc)
            seasons = get_actual_seasons()
            log.info("Polling %d seasons...", len(seasons))

            for season in seasons:
                for round_info in season.get("rounds", []):
                    start_time = datetime.fromisoformat(round_info["tradingTime"]["start"].replace("Z", "+00:00"))

                    rounds = process_round(season, round_info)
                    if not rounds:
                        continue

                    for round_data in rounds:
                        key = (round_data["round_id"], round_data["league"])
                        if key in processed_finished:
                            continue

                        has_results = all(m["result"] for m in round_data["matches"])
                        has_odds = any(m["odds"].get("1x2") for m in round_data["matches"])

                        if has_results:
                            save_round(round_data)
                            display_round(round_data)
                            save_standings(round_data["season_id"], round_data["league"])
                            display_standings(round_data["season_id"], round_data["league"])
                            processed_finished.add(key)
                            processed_upcoming.discard(key)
                        elif start_time > now and key not in processed_upcoming and has_odds:
                            save_round(round_data)
                            display_round(round_data)
                            processed_upcoming.add(key)

        except Exception as e:
            log.error("Monitor error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    monitor()
