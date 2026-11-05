#!/bin/bash
# Newest-first Gemma routing (17) then L1/L2 entity extraction (18), in rounds, resumable.
# Results land in data/gemma_ner.duckdb after every chunk. Stop cleanly: touch logs/gemma_ner.stop
cd "$(dirname "$0")/.."
PY=.venv/bin/python
LOG=logs/gemma_ner.log
rm -f logs/gemma_ner.stop
echo "start $(date -u +%FT%TZ)" >> $LOG
while [ ! -f logs/gemma_ner.stop ]; do
  $PY scripts/17_gemma_route.py --limit 500 --newest-first --apply --chunk 100 >> $LOG 2>&1 || { echo "17 failed" >> $LOG; break; }
  $PY scripts/18_gemma_entities.py --all --apply --chunk 100 >> $LOG 2>&1 || { echo "18 failed" >> $LOG; break; }
  n=$($PY - <<'P'
import duckdb
c=duckdb.connect('data/gemma_ner.duckdb',read_only=True)
print(c.execute("select count(*) from post_route_v1 where ok").fetchone()[0])
P
)
  echo "round done: $n posts routed $(date -u +%FT%TZ)" >> $LOG
  [ "$(grep -c 'to route (' $LOG | tail -1)" ] || true
  tail -n 12 $LOG | grep -q " 0 posts to route" && { echo "ALL DONE" >> $LOG; break; }
done
