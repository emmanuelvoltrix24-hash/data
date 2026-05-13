#!/usr/bin/env python3
"""
Global Collector Monitor
Runs all collectors in parallel threads and prints output to one terminal.
"""
import threading, subprocess, sys, os

COLLECTORS = {
    'betkraft':  ('python3', '/home/voltrix/collector.py'),
    'bongobongo':('python3', '/home/voltrix/bongobongo_collector.py'),
    'betpawa':   ('python3', '/home/voltrix/betpawa_collector.py'),
    'bangbet':   ('python3', '/home/voltrix/bangbet_collector.py'),
    'bet22':     ('python3', '/home/voltrix/bet22_collector.py'),
}

COLORS = {
    'betkraft':  '\033[94m',   # blue
    'bongobongo':'\033[92m',   # green
    'betpawa':   '\033[93m',   # yellow
    'bangbet':   '\033[95m',   # magenta
    'bet22':     '\033[96m',   # cyan
}
RESET = '\033[0m'
lock = threading.Lock()


def stream(name, cmd):
    color = COLORS.get(name, '')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env={**os.environ})
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                with lock:
                    print(f"{color}[{name}]{RESET} {line}", flush=True)
    except Exception as e:
        with lock:
            print(f"{color}[{name}]{RESET} ERROR: {e}", flush=True)


if __name__ == '__main__':
    print("🌍 Global Collector Monitor — all sources\n")
    threads = []
    for name, cmd in COLLECTORS.items():
        t = threading.Thread(target=stream, args=(name, cmd), daemon=True)
        t.start()
        threads.append(t)
        print(f"  Started: {name}")
    print()
    for t in threads:
        t.join()
