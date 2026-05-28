#!/usr/bin/env python3
"""
VFL Deploy Script — pushes to Railway or runs locally.
Usage:
    python3 deploy.py railway      # Push to Railway
    python3 deploy.py oracle       # Setup Oracle VPS
    python3 deploy.py local        # Run locally (all 5 collectors)
    python3 deploy.py local --betkraft --betpawa  # Run specific
"""
import os, sys, subprocess, json, shutil

REPO_DIR = '/home/voltrix/vfl-deploy'
GIT_REMOTE = 'origin'
GIT_BRANCH = 'main'


def check_git():
    """Ensure we're in the repo and can push."""
    os.chdir(REPO_DIR)
    result = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
    if result.stdout.strip():
        print("⚠️  Uncommitted changes. Committing...")
        subprocess.run(['git', 'add', '-A'], check=True)
        subprocess.run(['git', 'commit', '-m', 'deploy update'], check=True)
    return True


def deploy_railway():
    """Push to GitHub — Railway auto-deploys from there."""
    check_git()
    print("🚀 Pushing to Railway (via GitHub)...")
    subprocess.run(['git', 'push', GIT_REMOTE, GIT_BRANCH], check=True)
    print("✅ Pushed. Railway will auto-deploy.")
    print("   Check: https://railway.app/project/.../deployments")


def setup_oracle():
    """Setup Oracle VPS: supervisor config + nginx + systemd timer."""
    
    # ── Supervisor config for all 5 collectors ──
    supervisor_conf = """
[program:vfl-all]
command=python3 /home/voltrix/vfl-deploy/run_collectors.py --all
directory=/home/voltrix/vfl-deploy
user=voltrix
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/vfl/collector.err.log
stdout_logfile=/var/log/vfl/collector.out.log
environment=DATABASE_URL="postgresql://postgres:NZXmdfxWrCiCTRXFTaBJYUNhEtLvnUKP@nozomi.proxy.rlwy.net:13236/railway"
"""
    with open('/tmp/vfl_all.conf', 'w') as f:
        f.write(supervisor_conf.strip())

    # ── Health check systemd timer ──
    health_script = """#!/bin/bash
# VFL Health Check — run every 5 min via cron/systemd timer
PROCESSES=("local_collector" "bongobongo_collector" "betpawa_collector" "bangbet_collector" "bet22_collector")
for p in "${PROCESSES[@]}"; do
    if ! pgrep -f "$p" > /dev/null; then
        echo "$(date): $p not running — attempting restart"
        cd /home/voltrix/vfl-deploy
        nohup python3 "$p.py" >> /home/voltrix/vfl_data/logs/$p.log 2>&1 &
    fi
done
"""
    with open('/tmp/vfl_health.sh', 'w') as f:
        f.write(health_script.strip())

    print("""
╔══════════════════════════════════════════════════════════╗
║                  ORACLE VPS SETUP                        ║
╠══════════════════════════════════════════════════════════╣
║  Run these commands on Oracle:                           ║
╚══════════════════════════════════════════════════════════╝

# 1. Install supervisor
sudo apt install supervisor -y

# 2. Copy supervisor config
sudo cp /tmp/vfl_all.conf /etc/supervisor/conf.d/vfl_all.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start vfl-all

# 3. Setup health check
chmod +x /tmp/vfl_health.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /tmp/vfl_health.sh") | crontab -

# 4. Create log directory
mkdir -p /home/voltrix/vfl_data/logs

# 5. To check status:
supervisorctl status vfl-all

# 6. To stop:
supervisorctl stop vfl-all

# 7. To restart:
supervisorctl restart vfl-all
""")


def deploy_local(collectors=None):
    """Run locally using the runner or specified collectors."""
    os.chdir(REPO_DIR)
    if collectors:
        args = ' '.join(f'--{c}' for c in collectors)
        cmd = f'python3 run_collectors.py {args}'
    else:
        cmd = 'python3 run_collectors.py --all'
    print(f"🚀 Running: {cmd}")
    os.execvp('python3', ['python3', 'run_collectors.py'] + 
              (['--all'] if not collectors else [f'--{c}' for c in collectors]))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    extra = sys.argv[2:] if len(sys.argv) > 2 else []

    if command == 'railway':
        deploy_railway()
    elif command == 'oracle':
        setup_oracle()
    elif command == 'local':
        deploy_local(extra)
    else:
        print(f"Unknown: {command}")
        print(__doc__)


if __name__ == '__main__':
    main()
