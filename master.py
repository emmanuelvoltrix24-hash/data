#!/usr/bin/env python3
"""
VFL Master Process — Railway (NO AUTH)
Runs betkraft collector + learner + prediction engine in background threads.
Main thread runs the dashboard API.
"""
import threading, os, sys, time
from datetime import datetime, timezone, timedelta

# ── Background runner ────────────────────────────────────────────────

def run(name, fn):
    while True:
        try:
            print(f"[{name}] starting...", flush=True)
            fn()
        except Exception as e:
            print(f"[{name}] crashed: {e} — restarting in 10s", flush=True)
            time.sleep(10)

# ── Betkraft collector ───────────────────────────────────────────────

def start_betkraft():
    from railway_collector import collect, init_db
    init_db()
    collect()

# ── Prediction engine (no auth, reads DB) ───────────────────────────

def start_prediction_engine():
    import requests as req, json
    from db import get_db

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    LIVE_URL = 'https://vl.betkraft.co.uk/live'
    PERIODS_URL = 'https://vl.betkraft.co.uk/periods/1'

    def par(hg, ag):
        t = hg + ag
        return None if t == 0 else ('E' if t % 2 == 0 else 'O')

    def to_utc(s):
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) - timedelta(hours=3)

    RULES = {
        ('E','O','O'): {'rule':'E,O,O → M10 no loss','confidence':'HIGH','outcome':['W','D']},
        ('O','E','E'): {'rule':'O,E,E → M10 no loss','confidence':'HIGH','outcome':['W','D']},
        ('O','O','O'): {'rule':'O,O,O → M10 parity flips','confidence':'HIGH'},
        ('E','E','O'): {'rule':'E,E,O → M10 parity stable','confidence':'MEDIUM'},
        ('O','E','O'): {'rule':'O,E,O → M10 Even parity','confidence':'MEDIUM','parity':'E'},
    }

    seen = set()
    while True:
        try:
            r = req.get(PERIODS_URL, headers=HEADERS, timeout=10)
            periods = r.json()['data']['periods']
            now = datetime.now(timezone.utc)

            for period in periods:
                rid = period['round_number_id']
                if rid in seen:
                    continue
                start = to_utc(period['start_time'])
                wait = (start - now).total_seconds()
                if wait > 0:
                    time.sleep(max(0, wait))

                for _ in range(10):
                    payload = {k: period[k] for k in ('competition_id','end_time','round_number_id','start_time')}
                    r = req.post(LIVE_URL, json=payload, headers=HEADERS, timeout=10)
                    data = r.json()
                    if data.get('status_code') == 200 and data.get('data',{}).get('live'):
                        live = data['data']['live']
                        if len(live) == 10:
                            seen.add(rid)
                            matches = sorted(live, key=lambda m: m['event_id'])
                            slots = {}
                            for i, m in enumerate(matches, 1):
                                hg, ag = map(int, m['result'].split(':'))
                                slots[i] = {'n': i, 'home_team': m['home_team'], 'away_team': m['away_team'],
                                            'hg': hg, 'ag': ag, 'result': m['result'], 'parity': par(hg, ag)}
                            p5  = slots.get(5, {}).get('parity')
                            p6  = slots.get(6, {}).get('parity')
                            p7  = slots.get(7, {}).get('parity')
                            p10 = slots.get(10, {}).get('parity')
                            signal = RULES.get((p5, p6, p7), {'rule': 'No strong rule', 'confidence': 'LOW'})

                            from dashboard_api import engine_state
                            engine_state.update({
                                'last_round': rid,
                                'pattern': {'M5': p5, 'M6': p6, 'M7': p7, 'M10': p10},
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

# ── Learner ──────────────────────────────────────────────────────────

def start_learner():
    from db import get_db
    from global_learner import load_rounds, build_fvecs, mine_rules, save_rules, load_previous_rules, init_tables
    from collections import Counter
    import time as t

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
                    src_rounds = [(rd, s) for rd, s in rounds if s == src]
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

# ── Start everything ──────────────────────────────────────────────────

if __name__ == '__main__':
    collectors = [
        ('betkraft', start_betkraft),
        ('engine',   start_prediction_engine),
        ('learner',  start_learner),
    ]

    for name, fn in collectors:
        t = threading.Thread(target=run, args=(name, fn), daemon=True)
        t.start()
        print(f"Started: {name}", flush=True)

    port = int(os.environ.get('PORT', 8080))
    print(f"Dashboard on port {port}", flush=True)
    try:
        import gunicorn.app.base
        from dashboard_api import app

        class StandaloneApp(gunicorn.app.base.BaseApplication):
            def load_config(self):
                self.cfg.set('bind', f'0.0.0.0:{port}')
                self.cfg.set('workers', 1)
                self.cfg.set('timeout', 120)
            def load(self):
                return app
        StandaloneApp().run()
    except ImportError:
        from dashboard_api import app
        app.run(host='0.0.0.0', port=port)
