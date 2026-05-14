#!/usr/bin/env python3
"""
VFL P&L Tracker
Logs every bet placed and tracks wins/losses against actual results.
"""
import json, os, requests
from datetime import datetime

BETS_FILE   = 'data/bets.json'
LIVE_URL    = 'https://vl.betkraft.co.uk/live'
RESULTS_URL = 'https://vl.betkraft.co.uk/results/1/0'

BK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "tz": "UTC",
    "Referer": "https://legacy-ui.betkraft.co.uk/",
}

os.makedirs('data', exist_ok=True)


def load_bets():
    if os.path.exists(BETS_FILE):
        with open(BETS_FILE) as f:
            return json.load(f)
    return []

def save_bets(bets):
    with open(BETS_FILE, 'w') as f:
        json.dump(bets, f, indent=2)


def log_bet(event_id, round_id, match_desc, market_id, outcome_id, stake, odds, signal):
    """Call this when a bet is placed."""
    bets = load_bets()
    bets.append({
        'id': len(bets) + 1,
        'placed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'event_id': event_id,
        'round_id': round_id,
        'match': match_desc,
        'market': market_id,
        'outcome': outcome_id,
        'stake': stake,
        'odds': odds,
        'signal': signal,
        'result': None,   # filled in later
        'pnl': None,
        'status': 'pending',
    })
    save_bets(bets)
    print(f"  [pnl] Logged bet #{bets[-1]['id']}: {match_desc} {market_id}:{outcome_id} @{odds} stake={stake}")
    return bets[-1]['id']


def settle_bets():
    """Check pending bets against results and update P&L."""
    bets = load_bets()
    pending = [b for b in bets if b['status'] == 'pending']
    if not pending:
        return

    # Get recent results
    try:
        results = requests.get(RESULTS_URL, headers=BK_HEADERS, timeout=10).json()['data']['results']
        result_map = {}
        for rd in results:
            for m in rd.get('matches', []):
                result_map[m['event_id']] = m.get('result', '')
    except Exception as e:
        print(f"  [pnl] Results fetch failed: {e}")
        return

    settled = 0
    for bet in pending:
        result = result_map.get(bet['event_id'])
        if not result:
            continue

        hg, ag = map(int, result.split(':'))
        won = False

        if bet['market'] == '1X2':
            actual = 'W' if hg > ag else ('L' if hg < ag else 'D')
            won = (bet['outcome'] == '1' and actual == 'W') or \
                  (bet['outcome'] == 'X' and actual == 'D') or \
                  (bet['outcome'] == '2' and actual == 'L')
        elif bet['market'] == 'GG':
            both = hg > 0 and ag > 0
            won = (bet['outcome'] == 'Yes' and both) or (bet['outcome'] == 'No' and not both)
        elif bet['market'] == 'TGOE':
            total = hg + ag
            is_even = total % 2 == 0
            won = (bet['outcome'] == 'Even' and is_even) or (bet['outcome'] == 'Odd' and not is_even)
        elif bet['market'] in ('TG25', 'TG15', 'TG35'):
            total = hg + ag
            threshold = {'TG15': 1.5, 'TG25': 2.5, 'TG35': 3.5}[bet['market']]
            won = (bet['outcome'] == 'Over' and total > threshold) or \
                  (bet['outcome'] == 'Under' and total < threshold)

        bet['result'] = result
        bet['status'] = 'won' if won else 'lost'
        bet['pnl'] = round(bet['stake'] * (float(bet['odds']) - 1), 2) if won else -bet['stake']
        settled += 1

    if settled:
        save_bets(bets)
        print_summary(bets)


def print_summary(bets=None):
    if bets is None:
        bets = load_bets()
    settled = [b for b in bets if b['status'] != 'pending']
    pending = [b for b in bets if b['status'] == 'pending']
    won     = [b for b in settled if b['status'] == 'won']
    lost    = [b for b in settled if b['status'] == 'lost']
    total_staked = sum(b['stake'] for b in settled)
    total_pnl    = sum(b['pnl'] for b in settled)
    roi = total_pnl / total_staked * 100 if total_staked else 0

    print(f"\n{'─'*50}")
    print(f"  P&L SUMMARY")
    print(f"{'─'*50}")
    print(f"  Total bets : {len(bets)} ({len(pending)} pending)")
    print(f"  Won        : {len(won)}")
    print(f"  Lost       : {len(lost)}")
    print(f"  Win rate   : {len(won)*100//len(settled) if settled else 0}%")
    print(f"  Staked     : {total_staked} UGX")
    print(f"  P&L        : {total_pnl:+.0f} UGX")
    print(f"  ROI        : {roi:+.1f}%")
    print(f"{'─'*50}\n")

    # Last 10 bets
    print(f"  {'#':<4} {'Match':<25} {'Mkt':<8} {'Out':<6} {'Odds':<6} {'Stake':<6} {'Result':<8} {'P&L'}")
    for b in bets[-10:]:
        pnl = f"{b['pnl']:+.0f}" if b['pnl'] is not None else '-'
        print(f"  {b['id']:<4} {b['match'][:24]:<25} {b['market']:<8} {b['outcome']:<6} {b['odds']:<6} {b['stake']:<6} {b['status']:<8} {pnl}")


if __name__ == '__main__':
    settle_bets()
    print_summary()
