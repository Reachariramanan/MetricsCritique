#!/usr/bin/env bash
# Rebuild the metrics, the geo timeline tables and the dashboard snapshot, in order, stopping at the first failure.
#
#   bash scripts/run_rebuild.sh
#
# Steps (default switches of script 07, i.e. the standard metrics v2 behaviour):
#   0  repoint_embeddings_view.py   fix the post_embeddings view path for this machine/folder (no-op if already right)
#   1  07_metrics_v2.py             metrics v2 (posts + YouTube comments)      ~45-60 min, needs ~6-8 GB RAM at peak
#   2  08_metrics_db.py             publish to data/metrics.duckdb + parquet_metrics + results report
#   3  11_geo_timeline_metrics.py   entity / risk / theme daily metrics + alerts (data/geo_ontology.duckdb)
#   4  UI/build_dashboard.py        rebuild UI/index.html + UI/data/metrics.json
# Logs: logs/rebuild_<step>.log.   Afterwards (re)start the dashboard:  cd UI && ../.venv/bin/python3 api_server.py 8879
#
# Before you run it: stop the dashboard server / container and any other stage that has a data/*.duckdb open
# (DuckDB allows only one writer). Override the interpreter with:  PY=/path/to/python bash scripts/run_rebuild.sh
# Resume after a failed step WITHOUT repeating the earlier ones (07 takes ~45 min):
#   FROM=08 bash scripts/run_rebuild.sh     # runs 08, 11 and the dashboard build   (FROM=11 -> 11 + dashboard, etc.)
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
mkdir -p logs

# ---- pre-flight ---------------------------------------------------------------------------------------------
avail_mb=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
echo "python : $PY ($($PY --version 2>&1))"
echo "RAM    : ${avail_mb} MB available (script 07 needs about 6000-8000 MB at its peak)"
if [ "$avail_mb" -gt 0 ] && [ "$avail_mb" -lt 8000 ]; then
  echo "WARNING: less than 8 GB free - script 07 may run out of memory on a busy machine. Continuing in 10 s (Ctrl+C to stop)."
  sleep 10
fi
if command -v fuser >/dev/null 2>&1; then
  for f in data/analytics.duckdb data/metrics.duckdb data/geo_ontology.duckdb; do
    if [ -e "$f" ] && fuser "$f" >/dev/null 2>&1; then
      echo "ERROR: $f is open in another process (dashboard server / container / another stage). Stop it and re-run."
      exit 1
    fi
  done
fi

run() {
  local name="$1"; shift
  local log="logs/rebuild_${name}.log"
  if [ -n "${FROM:-}" ] && [[ "$name" < "$FROM" ]]; then
    echo "[$(date +%H:%M:%S)] SKIP  ${name}  (FROM=${FROM})"
    return 0
  fi
  echo "[$(date +%H:%M:%S)] START ${name}  (log: $log)"
  if "$@" >"$log" 2>&1; then
    echo "[$(date +%H:%M:%S)] OK    ${name}"
  else
    echo "[$(date +%H:%M:%S)] FAILED ${name} - last lines of $log:"
    tail -n 25 "$log"
    exit 1
  fi
}

run 00_view   "$PY" scripts/repoint_embeddings_view.py
run 07_metrics "$PY" scripts/07_metrics_v2.py
run 08_publish "$PY" scripts/08_metrics_db.py
run 11_geo     "$PY" scripts/11_geo_timeline_metrics.py
run 99_dashboard "$PY" UI/build_dashboard.py

echo
echo "ALL DONE $(date '+%F %T')"
echo "Start the dashboard:  cd UI && ../.venv/bin/python3 api_server.py 8879   (then open http://127.0.0.1:8879/)"
