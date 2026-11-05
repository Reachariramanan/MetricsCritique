"""Contract check for GET /api/gemma/status logic (UI/gemma_live.py) against fixture duckdb files.

Env overrides used: GEMMA_NER_DB, INCOMING_DB, ANALYTICS_DB, ENTITY_SCHEMA_JSON.
Run: .venv/bin/python UI/tests/check_live_ner.py
"""
import os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from live_ner_fixture import build
import gemma_live

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

tmp = Path(os.environ.get("LIVE_NER_TMP") or tempfile.mkdtemp(prefix="live_ner_"))
env = build(tmp); os.environ.update(env)

d = gemma_live.gemma_status(use_cache=False)
check("available", d.get("available") is True)
for k in ("updated_at routed extracted_posts entities ok_rate rate_per_min incoming_pending by_domain "
          "domain_names by_level top_entities recent watcher").split():
    check(f"key {k}", k in d)
check("routed=4", d["routed"] == 4)
check("ok_rate=0.75", abs(d["ok_rate"] - 0.75) < 1e-9)
check("rate_per_min=0.3 (3 in 10 min)", abs(d["rate_per_min"] - 0.3) < 1e-9)
check("extracted_posts=3", d["extracted_posts"] == 3)
check("entities=4", d["entities"] == 4)
check("by_domain", d["by_domain"] == {"D01": 2, "D02": 1})
check("domain_names", d["domain_names"] == {"D01": "Conflict Fixture", "D02": "Economy Fixture"})
check("by_level", d["by_level"] == {"L1": 1, "L2": 3})
check("top_entities 24h excludes stale", [e["name"] for e in d["top_entities"]] == ["Acme", "Bob"] and d["top_entities"][0]["n"] == 2)
check("incoming_pending=2 (n2,b2)", d["incoming_pending"] == 2)
check("watcher", d["watcher"]["new_seen_total"] == 12 and d["watcher"]["last_poll"])
r = {x["post_uid"]: x for x in d["recent"]}
check("recent newest first", [x["post_uid"] for x in d["recent"]] == ["n1", "b1", "b2", "old"])
check("n1 source=new", r["n1"]["source"] == "new")
check("b1 source=backlog", r["b1"]["source"] == "backlog")
check("snippet <=180", len(r["n1"]["snippet"]) == 180)
check("backlog snippet from post_facts", r["b1"]["snippet"] == "backlog text b1")
check("entities attached", {e["name"] for e in r["n1"]["entities"]} == {"Acme", "Bob"})
check("domain_2 passthrough", r["b1"]["domain_2"] == "D01")

# cache: 3s memoisation
d1 = gemma_live.gemma_status(); d2 = gemma_live.gemma_status()
check("cached within 3s", d1 is d2)

# missing-file path never raises
os.environ["INCOMING_DB"] = str(tmp / "nope.duckdb")
m = gemma_live.gemma_status(use_cache=False)
check("missing incoming -> available=false + reason", m["available"] is False and "nope" in m["error"])
os.environ["INCOMING_DB"] = env["INCOMING_DB"]; os.environ["GEMMA_NER_DB"] = str(tmp / "nada.duckdb")
m = gemma_live.gemma_status(use_cache=False)
check("missing gemma -> available=false + reason", m["available"] is False and "nada" in m["error"])

print(f"\n{'FAILED' if fails else 'OK'}: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
