#!/usr/bin/env python3
"""Generate `data/entity_schema.json` from `docs/entities/ENTITIES_EXTRACTED_Final.md`.

The markdown is the human-reviewed source of truth for which entity types exist.
The Gemma routing/extraction stages (17, 18) need it machine-readable, so this
script parses Part C of the document once:

  L1 = the "Core entities (all domains D01-D09)" table  -> asked for on every post
  L2 = each "D0x <name> (<bucket> bucket)" table        -> asked for only when the
                                                           post routes to that domain

Run it after the markdown changes and commit the JSON (it is hand-authored config,
not corpus data; `.gitignore` whitelists it).

    python scripts/build_entity_schema.py            # write data/entity_schema.json
    python scripts/build_entity_schema.py --check    # exit 1 if the JSON is stale
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "entities" / "ENTITIES_EXTRACTED_Final.md"
OUT = ROOT / "data" / "entity_schema.json"

SCHEMA_VERSION = 1
_DOMAIN_H = re.compile(r"^###\s+(D0\d)\s+(.+?)\s+\((.+?) bucket\)\s*$")
_CORE_H = re.compile(r"^###\s+Core entities")
_ROW = re.compile(r"^\|\s*`([A-Z0-9_]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$")
_MAP_ROW = re.compile(r"^\|\s*(D0\d)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
_SKIP = re.compile(r"\*\*Skip \(already extracted\):\*\*\s*(.+)")
_SKIP_TYPE = re.compile(r"`([A-Z0-9_]+)`")


def parse(md: str) -> dict:
    domains: dict[str, dict] = {}
    core: list[dict] = []
    core_skipped: list[str] = []
    cur = None  # None | "core" | domain id
    in_part_c = False
    for line in md.splitlines():
        if line.startswith("## 4. Part C"):
            in_part_c = True
            continue
        if line.startswith("## ") and in_part_c and not line.startswith("## 4."):
            break
        if not in_part_c:
            continue
        if _CORE_H.match(line):
            cur = "core"
            continue
        m = _DOMAIN_H.match(line)
        if m:
            did, name, bucket = m.groups()
            domains[did] = {"name": name, "bucket": bucket, "types": [], "skipped": []}
            cur = did
            continue
        if cur is None:
            continue
        sk = _SKIP.search(line)
        if sk:
            tgt = core_skipped if cur == "core" else domains[cur]["skipped"]
            # keep type codes (UPPER_SNAKE); lowercase names are pipeline fields
            tgt.extend(t for t in _SKIP_TYPE.findall(sk.group(1)) if t.isupper() and "_" in t)
            continue
        r = _ROW.match(line)
        if r:
            etype, tier, capture, status, action = r.groups()
            row = {"type": etype, "tier": tier, "capture": capture, "status": status,
                   "action": action}
            if cur == "core":
                core.append(row)
            else:
                domains[cur]["types"].append(row)

    # The core "skip" line names EVT_DATE, which is still an L1 entity for routing.
    for t in core_skipped:
        if not any(c["type"] == t for c in core):
            core.append({"type": t, "tier": "A", "capture": "Event date",
                         "status": "Covered", "action": "already extracted (incident_date)"})
    return {"core": core, "domains": domains}


def domain_buckets(md: str) -> dict[str, str]:
    out = {}
    in_sec1 = False
    for line in md.splitlines():
        if line.startswith("## 1. Domain to bucket map"):
            in_sec1 = True
            continue
        if in_sec1 and line.startswith("## "):
            break
        if in_sec1:
            m = _MAP_ROW.match(line)
            if m:
                out[m.group(1)] = m.group(3)
    return out


def build() -> dict:
    md = SRC.read_text(encoding="utf-8")
    parsed = parse(md)
    buckets = domain_buckets(md)
    for did, d in parsed["domains"].items():
        d["bucket"] = buckets.get(did, d["bucket"])
        seen, uniq = set(), []
        for t in d["types"]:           # a type repeats inside a domain at several tiers
            if t["type"] not in seen:
                seen.add(t["type"])
                uniq.append(t)
        d["types"] = uniq
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "docs/entities/ENTITIES_EXTRACTED_Final.md (Part C)",
        "l1": parsed["core"],
        "l2": parsed["domains"],
        "domain_ids": sorted(parsed["domains"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    schema = build()
    text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("entity_schema.json is stale: rerun scripts/build_entity_schema.py")
            return 1
        print("entity_schema.json is current")
        return 0
    OUT.write_text(text, encoding="utf-8")
    n_l2 = sum(len(d["types"]) for d in schema["l2"].values())
    print(f"wrote {OUT.relative_to(ROOT)}: {len(schema['l1'])} L1 types, "
          f"{len(schema['l2'])} domains, {n_l2} L2 type rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
