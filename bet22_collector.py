#!/usr/bin/env python3
"""
22bet Virtual Football Collector - STEALTH Edition
Polls 22bet/1xBet Virtuals API with improved evasion and session handling.
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

# Target URL for England Premier League (champs=88637)
BASE_URL = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip"
PARAMS = {
    "champs": "88637",
    "count": "50",
    "lng": "en_GB",
    "tf": "3000000",
    "mode": "4",
    "country": "191",
    "partner": "151",
    "getEmpty": "true",
    "gr": "337"
}

# Rotated User-Agents for better stealth
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
]

SAVE_DIR = "/home/voltrix/bet22_vfl_data"
os.makedirs(SAVE_DIR, exist_ok=True)
POLL_INTERVAL = 30


class StealthSession:
    """Handles requests with persistent session and random headers to avoid EOF/403."""
    def __init__(self):
        self.session = requests.Session()
        self.update_headers()

    def update_headers(self):
        self.session.headers.update({
            "user-agent": random.choice(USER_AGENTS),
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "referer": "https://22bet.ug/virtualsports",
            "origin": "https://22bet.ug",
            "connection": "keep-alive",
            "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin"
        })

    def get(self, url, params):
        try:
            # Random slight delay before request
            time.sleep(random.uniform(0.5, 2.0))
            r = self.session.get(url, params=params, timeout=20)
            
            if r.status_code == 403:
                log.warning("403 Forbidden. Rotating session...")
                self.session = requests.Session()
                self.update_headers()
                return None
                
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError as e:
            log.error("SSL/Protocol error (Potential detection): %s", e)
            # Re-init session on SSL EOF
            self.session = requests.Session()
            self.update_headers()
            return None
        except Exception as e:
            log.warning("Fetch failed: %s", e)
            return None


def parse_all_markets(match):
    mapped = {"1X2": {}, "DC": {}, "BTTS": {}, "OU": {}, "Handicap": [], "IndividualTotal": {}}
    raw_events = []
    events = []
    if "E" in match: events.extend(match["E"])
    if "AE" in match:
        for ae_group in match["AE"]:
            if "ME" in ae_group: events.extend(ae_group["ME"])

    for e in events:
        g, t, c, p = e.get("G"), e.get("T"), e.get("C"), e.get("P")
        raw_events.append({"G": g, "T": t, "C": c, "P": p})
        if g == 1:
            if t == 1: mapped["1X2"]["H"] = c
            elif t == 2: mapped["1X2"]["D"] = c
            elif t == 3: mapped["1X2"]["A"] = c
        elif g == 8:
            if t == 4: mapped["DC"]["1X"] = c
            elif t == 5: mapped["DC"]["12"] = c
            elif t == 6: mapped["DC"]["X2"] = c
        elif g == 17:
            line = str(p)
            if line not in mapped["OU"]: mapped["OU"][line] = {}
            if t == 9: mapped["OU"][line]["Over"] = c
            elif t == 10: mapped["OU"][line]["Under"] = c
        elif g == 19:
            if t == 180: mapped["BTTS"]["Yes"] = c
            elif t == 181: mapped["BTTS"]["No"] = c
        elif g == 2: mapped["Handicap"].append({"P": p, "T": t, "C": c})
        elif g in [15, 62]:
            team = "Home" if g == 15 else "Away"
            line = str(p)
            if team not in mapped["IndividualTotal"]: mapped["IndividualTotal"][team] = {}
            if line not in mapped["IndividualTotal"][team]: mapped["IndividualTotal"][team][line] = {}
            if t in [11, 13]: mapped["IndividualTotal"][team][line]["Over"] = c
            elif t in [12, 14]: mapped["IndividualTotal"][team][line]["Under"] = c
    return mapped, raw_events


def process_response(data):
    if not data or "Value" not in data: return []
    rounds = {}
    for item in data["Value"]:
        round_name = item.get("MIO", {}).get("TSt", "Unknown Round")
        league = item.get("L", "Unknown League")
        res_obj = item.get("SC", {})
        hg, ag = None, None
        if "FS" in res_obj:
            hg, ag = res_obj["FS"].get("S1"), res_obj["FS"].get("S2")
        mapped_odds, raw_markets = parse_all_markets(item)
        match_data = {
            "id": item.get("I"), "home": item.get("O1"), "away": item.get("O2"),
            "odds": mapped_odds, "raw_markets": raw_markets,
            "result": f"{hg}:{ag}" if hg is not None and ag is not None else None,
            "hg": hg, "ag": ag, "start_time": item.get("S"), "win_prob": item.get("WP")
        }
        key = (round_name, league)
        if key not in rounds:
            rounds[key] = {"round_name": round_name, "league": league, "timestamp": item.get("S"), "matches": []}
        rounds[key]["matches"].append(match_data)
    return list(rounds.values())


def save_round(round_data):
    safe_name, safe_league = round_data["round_name"].replace(" ", "_"), round_data["league"].replace(" ", "_").replace(".", "")
    filename = f"{SAVE_DIR}/{safe_league}_{safe_name}.json"
    with open(filename, "w") as f: json.dump(round_data, f, indent=2)
    # Write to unified DB
    try:
        from db import save_round as db_save_round
        db_matches = [{'n': i+1, 'home': m['home'], 'away': m['away'],
                       'hg': m.get('hg') or 0, 'ag': m.get('ag') or 0,
                       'result': m.get('result') or '',
                       'odds': {'1x2': m.get('odds', {}).get('1x2', {})}}
                      for i, m in enumerate(round_data['matches'])]
        db_save_round(str(round_data['round_id']), 'bet22', round_data.get('league',''), db_matches)
    except Exception as e:
        pass  # silent — DB optional for bet22
    return filename


def display_round(data):
    ts = datetime.now().strftime('%H:%M:%S')
    log.info("STEALTH CAPTURE [%s] | League: %s | Round: %s", ts, data["league"], data["round_name"])
    header = f"  {'Home':<25} | {'Away':<25} | {'1X2 (H/D/A)':^20} | {'O/U 2.5':^14} | {'BTTS':^10} | {'Res':^5}"
    print("\n" + header + "\n  " + "-" * len(header))
    for m in data["matches"]:
        o = m["odds"]
        h, d, a = f"{o['1X2'].get('H','-'):>5}", f"{o['1X2'].get('D','-'):>5}", f"{o['1X2'].get('A','-'):>5}"
        ou25 = o["OU"].get("2.5", {}); ov, un = ou25.get('Over', '-'), ou25.get('Under', '-')
        b_y, b_n = o['BTTS'].get('Yes', '-'), o['BTTS'].get('No', '-')
        res = m["result"] or " - "
        print(f"  {m['home']:<25} | {m['away']:<25} | {f'{h} {d} {a}':^20} | {f'{ov:>5} / {un:<5}':^14} | {f'{b_y:>4}/{b_n:<4}':^10} | {res:^5}")
    print("  " + "=" * len(header) + "\n")


def monitor():
    session = StealthSession()
    processed_keys = set()
    log.info("22bet STEALTH Collector started.")

    while True:
        try:
            data = session.get(BASE_URL, PARAMS)
            if not data:
                time.sleep(10)
                continue
                
            rounds = process_response(data)
            for r in rounds:
                res_fp = "-".join([str(m["result"]) for m in r["matches"]])
                odds_fp = sum([len(m["raw_markets"]) for m in r["matches"]])
                key = f"{r['league']}_{r['round_name']}_{res_fp}_{odds_fp}"
                if key not in processed_keys:
                    save_round(r); display_round(r); processed_keys.add(key)
                    if len(processed_keys) > 2000: processed_keys.clear()

        except Exception as e:
            log.error("Monitor error: %s", e)

        # Added jitter to polling interval
        time.sleep(POLL_INTERVAL + random.uniform(5, 15))


if __name__ == "__main__":
    monitor()
