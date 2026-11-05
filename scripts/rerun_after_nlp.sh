#!/usr/bin/env bash
# Waits for 04_nlp_scores.py, then rebuilds metrics v2 + dashboard and restarts the server.
# Usage: scripts/rerun_after_nlp.sh   (run from anywhere; logs go to logs/rerun_*.log)
set -u
cd "$(dirname "$0")/.."
while pgrep -f "[0]4_nlp_scores.py" >/dev/null; do sleep 30; done
if ! grep -q "wrote\|complete\|done" logs/rerun_04.log 2>/dev/null; then
  echo "04 did not finish cleanly; see logs/rerun_04.log" > logs/rerun_chain.log; exit 1
fi
.venv/bin/python scripts/07_metrics_v2.py > logs/rerun_07.log 2>&1 || { echo "07 failed" > logs/rerun_chain.log; exit 1; }
.venv/bin/python scripts/08_metrics_db.py > logs/rerun_08.log 2>&1 || { echo "08 failed" > logs/rerun_chain.log; exit 1; }
.venv/bin/python UI/build_dashboard.py   > logs/rerun_build.log 2>&1 || { echo "build failed" > logs/rerun_chain.log; exit 1; }
(cd UI && setsid nohup ../.venv/bin/python3 api_server.py 8877 > ../logs/dashboard_server.log 2>&1 < /dev/null &)
echo "all done $(date -u +%FT%TZ)" > logs/rerun_chain.log
