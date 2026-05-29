# Railway Procfile
# Web: runs 5 collectors + Flask health endpoint
web: python railway_runner.py
# Worker: runs the learner in a loop (re-mines every 30 min)
worker: while true; do python run_learner.py; sleep 1800; done
