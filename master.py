#!/usr/bin/env python3
"""
VFL Master Process — Railway
Runs all collectors + learner in background threads.
Main thread runs the dashboard API (web process).
"""
import threading, os, sys, time

# ── Auto-login on startup if cookies missing ──────────────────────────────────
COOKIES_FILE = os.path.join(os.path.dirname(__file__), 'bandabets_cookies.json')
if not os.path.exists(COOKIES_FILE):
    print("No cookies found — running auto-login...", flush=True)
    try:
        from auth import ensure_session
        ensure_session()
        print("Auto-login complete", flush=True)
    except Exception as e:
        print(f"Auto-login failed: {e}", flush=True)

def run(name, fn):
    while True:
        try:
            print(f"[{name}] starting...", flush=True)
            fn()
        except Exception as e:
            print(f"[{name}] crashed: {e} — restarting in 10s", flush=True)
            time.sleep(10)

def start_betkraft():
    from railway_collector import collect, init_db
    init_db()
    collect()

def start_prediction_engine():
    import requests as req, time
    from datetime import datetime, timezone
    from auth import ensure_session, load_cookies

    PERIODS_URL = 'https://vl.betkraft.co.uk/periods/1'
    LIVE_URL    = 'https://vl.betkraft.co.uk/live'

    def par(hg, ag): t=hg+ag; return None if t==0 else ('E' if t%2==0 else 'O')
    def to_utc(s): return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

    RULES = {
        ('E','O','O'): {'rule':'E,O,O → M10 no loss','confidence':'HIGH','outcome':['W','D']},
        ('O','E','E'): {'rule':'O,E,E → M10 no loss','confidence':'HIGH','outcome':['W','D']},
        ('O','O','O'): {'rule':'O,O,O → M10 parity flips','confidence':'HIGH'},
        ('E','E','O'): {'rule':'E,E,O → M10 parity stable','confidence':'MEDIUM'},
        ('O','E','O'): {'rule':'O,E,O → M10 Even parity','confidence':'MEDIUM','parity':'E'},
    }

    ensure_session()
    seen = set()

    while True:
        try:
            cookies = load_cookies()
            periods = req.get(PERIODS_URL, cookies=cookies, timeout=10).json()['data']['periods']
            now = datetime.now(timezone.utc)

            for period in periods:
                rid = period['round_number_id']
                if rid in seen: continue
                wait = (to_utc(period['start_time']) - now).total_seconds()
                if wait > 0: time.sleep(max(0, wait))

                for _ in range(10):
                    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
                    r = req.post(LIVE_URL, cookies=load_cookies(), json=payload, timeout=10).json()
                    if r.get('status_code') == 200 and r['data']:
                        live = r['data'].get('live', [])
                        if len(live) == 10:
                            seen.add(rid)
                            matches = sorted(live, key=lambda m: m['event_id'])
                            slots = {}
                            for i, m in enumerate(matches, 1):
                                hg, ag = map(int, m['result'].split(':'))
                                slots[i] = {'n':i,'home_team':m['home_team'],'away_team':m['away_team'],
                                            'hg':hg,'ag':ag,'result':m['result'],'parity':par(hg,ag)}
                            p5  = slots.get(5,{}).get('parity')
                            p6  = slots.get(6,{}).get('parity')
                            p7  = slots.get(7,{}).get('parity')
                            p10 = slots.get(10,{}).get('parity')
                            signal = RULES.get((p5,p6,p7), {'rule':'No strong rule','confidence':'LOW'})

                            from dashboard_api import engine_state
                            engine_state.update({
                                'last_round': rid,
                                'pattern': {'M5':p5,'M6':p6,'M7':p7,'M10':p10},
                                'signal': signal,
                                'last_updated': datetime.now().strftime('%H:%M:%S'),
                            })
                            print(f"[engine] #{rid} ({p5},{p6},{p7}) → {signal['confidence']}", flush=True)
                            break
                    time.sleep(2)
                break
        except Exception as e:
            print(f"[engine] error: {e}", flush=True)
        time.sleep(5)

def start_prediction_engine():
    """Run prediction engine and update dashboard state."""
    import requests as req, json, time
    from datetime import datetime, timezone, timedelta
    from auth import ensure_session, load_cookies

    PERIODS_URL = 'https://vl.betkraft.co.uk/periods/1'
    LIVE_URL    = 'https://vl.betkraft.co.uk/live'

    def par(hg, ag): t=hg+ag; return None if t==0 else ('E' if t%2==0 else 'O')
    def to_utc(s):
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

    ensure_session()
    seen = set()
    prev_round = None

    while True:
        try:
            cookies = load_cookies()
            periods = req.get(PERIODS_URL, cookies=cookies, timeout=10).json()['data']['periods']
            now = datetime.now(timezone.utc)

            for period in periods:
                rid = period['round_number_id']
                if rid in seen: continue
                start = to_utc(period['start_time'])
                wait = (start - now).total_seconds()
                if wait > 0:
                    time.sleep(max(0, wait))

                for _ in range(10):
                    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
                    r = req.post(LIVE_URL, cookies=load_cookies(), json=payload, timeout=10).json()
                    if r.get('status_code') == 200 and r['data']:
                        live = r['data'].get('live', [])
                        if len(live) == 10:
                            seen.add(rid)
                            matches = sorted(live, key=lambda m: m['event_id'])
                            parsed = []
                            for i, m in enumerate(matches, 1):
                                hg, ag = map(int, m['result'].split(':'))
                                parsed.append({'n':i,'home_team':m['home_team'],'away_team':m['away_team'],
                                               'hg':hg,'ag':ag,'result':m['result'],'parity':par(hg,ag)})
                            prev_round = {'round_id': rid, 'matches': parsed}

                            # Compute pattern
                            slots = {m['n']: m for m in parsed}
                            p5  = slots.get(5,{}).get('parity')
                            p6  = slots.get(6,{}).get('parity')
                            p7  = slots.get(7,{}).get('parity')
                            p10 = slots.get(10,{}).get('parity')

                            rules_map = {
                                ('E','O','O'): {'rule':'E,O,O → M10 no loss','confidence':'HIGH','outcome':['W','D']},
                                ('O','E','E'): {'rule':'O,E,E → M10 no loss','confidence':'HIGH','outcome':['W','D']},
                                ('O','O','O'): {'rule':'O,O,O → M10 parity flips','confidence':'HIGH'},
                                ('E','E','O'): {'rule':'E,E,O → M10 parity stable','confidence':'MEDIUM'},
                                ('O','E','O'): {'rule':'O,E,O → M10 Even parity','confidence':'MEDIUM','parity':'E'},
                            }
                            signal = rules_map.get((p5,p6,p7), {'rule':'No strong rule','confidence':'LOW'})

                            from dashboard_api import engine_state
                            engine_state.update({
                                'last_round': rid,
                                'pattern': {'M5':p5,'M6':p6,'M7':p7,'M10':p10},
                                'signal': signal,
                                'last_updated': datetime.now().strftime('%H:%M:%S'),
                            })
                            print(f"[engine] #{rid} ({p5},{p6},{p7}) → {signal['confidence']}", flush=True)
                            break
                    time.sleep(2)
                break
        except Exception as e:
            print(f"[engine] error: {e}", flush=True)
        time.sleep(5)

def start_bongobongo():
    from bongobongo_collector import monitor
    monitor()

def start_betpawa():
    from betpawa_collector import monitor
    monitor()

def start_bangbet():
    from bangbet_collector import monitor
    monitor()

def start_bet22():
    from bet22_collector import monitor
    monitor()

def start_learner():
    # inline learner loop to avoid import issues
    import time as t
    from global_learner import init_tables, load_rounds, build_fvecs, mine_rules, save_rules, load_previous_rules
    from collections import Counter
    from datetime import datetime
    init_tables()
    while True:
        try:
            rounds = load_rounds()
            src_counts = Counter(s for _, s in rounds)
            print(f"[learner] {len(rounds)} rounds — {dict(src_counts)}", flush=True)
            if len(rounds) >= 30:
                prev = load_previous_rules()
                fvecs, sources_used = build_fvecs(rounds)
                for src in src_counts:
                    src_rounds = [(rd,s) for rd,s in zip([r for r,_ in rounds],[s for _,s in rounds]) if s==src]
                    if len(src_rounds) >= 30:
                        fv2, su2 = build_fvecs(list(zip(src_rounds, [src]*len(src_rounds))))
                        rules = mine_rules(fv2, su2)
                        save_rules(rules, {}, len(src_rounds), source=src)
                rules = mine_rules(fvecs, sources_used)
                save_rules(rules, prev, len(rounds), source='all')
                print(f"[learner] saved {len(rules)} global rules", flush=True)
        except Exception as e:
            print(f"[learner] error: {e}", flush=True)
        t.sleep(300)


if __name__ == '__main__':
    collectors = [
        ('betkraft',   start_betkraft),
        ('bongobongo', start_bongobongo),
        ('betpawa',    start_betpawa),
        ('bangbet',    start_bangbet),
        ('learner',    start_learner),
        ('engine',     start_prediction_engine),
    ]

    for name, fn in collectors:
        t = threading.Thread(target=run, args=(name, fn), daemon=True)
        t.start()
        print(f"Started: {name}", flush=True)

    # Main thread: run dashboard API
    from dashboard_api import app
    port = int(os.environ.get('PORT', 8080))
    print(f"Dashboard on port {port}", flush=True)
    app.run(host='0.0.0.0', port=port)
