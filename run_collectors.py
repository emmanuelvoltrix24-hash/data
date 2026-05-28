#!/usr/bin/env python3
"""
VFL Collector Runner — launch any combination of collectors.
Usage:
    python3 run_collectors.py --all              # Run all 5
    python3 run_collectors.py --betkraft         # Only betkraft
    python3 run_collectors.py --betpawa --bangbet  # Only betpawa + bangbet
    python3 run_collectors.py --list             # List available collectors
"""
import sys, os, threading, time, signal, argparse
from datetime import datetime

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Ensure DB schema exists
from db_writer import init_db
init_db()

COLLECTORS = {
    'betkraft': {
        'module': 'local_collector',
        'func': 'collect',
        'description': 'Betkraft — 10 matches, 27 markets, standings',
        'color': '\033[94m',  # blue
    },
    'bongobongo': {
        'module': 'bongobongo_collector',
        'func': 'collect',
        'description': 'BongoBongo — 10 matches, 1X2 odds, standings',
        'color': '\033[92m',  # green
    },
    'betpawa': {
        'module': 'betpawa_collector',
        'func': 'collect',
        'description': 'BetPawa — 66 events, 5 markets, 7 leagues',
        'color': '\033[93m',  # yellow
    },
    'bangbet': {
        'module': 'bangbet_collector',
        'func': 'collect',
        'description': 'BangBet — results + HT, 8 tournaments',
        'color': '\033[95m',  # magenta
    },
    'bet22': {
        'module': 'bet22_collector',
        'func': 'collect',
        'description': '22Bet — full odds (1X2/DC/BTTS/OU/Handicap)',
        'color': '\033[96m',  # cyan
    },
}

ENDC = '\033[0m'
running_threads = {}
stop_event = threading.Event()


def run_collector(name, info):
    """Import and run a collector in a thread."""
    try:
        mod = __import__(info['module'])
        print(f"{info['color']}[runner] Starting {name}...{ENDC}", flush=True)
        info['func'] = mod.collect
        info['func']()
    except Exception as e:
        print(f"{info['color']}[runner] {name} crashed: {e}{ENDC}", flush=True)
        import traceback
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description='VFL Collector Runner')
    parser.add_argument('--all', action='store_true', help='Run all collectors')
    parser.add_argument('--list', action='store_true', help='List available collectors')
    parser.add_argument('--betkraft', action='store_true')
    parser.add_argument('--bongobongo', action='store_true')
    parser.add_argument('--betpawa', action='store_true')
    parser.add_argument('--bangbet', action='store_true')
    parser.add_argument('--bet22', action='store_true')
    args = parser.parse_args()

    if args.list:
        print("\nAvailable collectors:")
        for name, info in COLLECTORS.items():
            print(f"  --{name:<12} {info['description']}")
        print()
        return

    # Determine which to run
    to_run = []
    if args.all:
        to_run = list(COLLECTORS.keys())
    else:
        for name in COLLECTORS:
            if getattr(args, name, False):
                to_run.append(name)

    if not to_run:
        parser.print_help()
        print("\nNo collectors specified. Use --all or pick specific ones (e.g. --betkraft --betpawa)")
        return

    print(f"\n{'='*50}")
    print(f"  VFL Collectors — starting {len(to_run)} sources")
    print(f"  Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")

    threads = []
    for name in to_run:
        info = COLLECTORS[name]
        t = threading.Thread(target=run_collector, args=(name, info), daemon=True)
        t.start()
        threads.append(t)
        print(f"{info['color']}  🔵 {name:<12} started{ENDC}", flush=True)

    print(f"\n  {len(to_run)} collectors running. Press Ctrl+C to stop all.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nShutting down all collectors...")
        stop_event.set()
        print("Done.")
        sys.exit(0)


if __name__ == '__main__':
    main()
