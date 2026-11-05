#!/usr/bin/env python3
"""Extract L1 + L2 entities with the local Gemma, using the domain chosen by script 17.

Second half of the NER rewrite (plan Workstream D). For each routed post the prompt
lists the L1 types (core: locations, initiator, target, mode, outcome, date) plus the L2
types of the post's domain (and of its runner-up domain, if any), taken from
`data/entity_schema.json` (built from docs/entities/ENTITIES_EXTRACTED_Final.md). A post
routed to `none` gets L1 only.

Every mention keeps a verbatim evidence quote, checked against the post text
(`evidence_ok`); a quote that is not in the post is flagged, not trusted.

Output tables (new, versioned; wikineural `entity_dim` / `entity_mentions` untouched):
  entity_mentions_v2     one row per mention
  entity_dim_v2          one row per canonical entity (rebuilt from the mentions)
  post_entities_status_v2  one row per post tried (ok/error/tokens) -> resume + failure rate

Entity ids: gazetteer hits reuse `data/gazetteer.json` ids (`uid_source='gazetteer'`);
all others get a deterministic id from (type, normalised canonical name)
(`uid_source='derived'`). Merging aliases beyond that is a later, measured step.

USAGE
-----
    python scripts/18_gemma_entities.py               # pilot: 200 routed posts, report only
    python scripts/18_gemma_entities.py --limit 5000 --apply
    python scripts/18_gemma_entities.py --all --apply # resumable

Needs script 17's `post_route_v1`. Reads `analytics.duckdb` read-only; writes only
`gemma_ner.duckdb`, and only with --apply (stop `UI/api_server.py` first).
Gate before use: ~120 hand-judged posts, per-type precision/recall, per language, against
wikineural on the same sample (plan Workstream D-b, D-c).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import entity_routing as R  # noqa: E402
from lib import gemma_client as G  # noqa: E402
from lib.config import DATA_DIR, DUCKDB_PATH, GEMMA_DB_PATH, gemma_config  # noqa: E402

MENTIONS, DIM, STATUS = "entity_mentions_v2", "entity_dim_v2", "post_entities_status_v2"
DDL = {
    MENTIONS: f"""CREATE TABLE IF NOT EXISTS {MENTIONS} (
        mention_id VARCHAR, post_uid VARCHAR, level VARCHAR, domain VARCHAR,
        entity_type VARCHAR, name VARCHAR, canonical_name VARCHAR,
        entity_uid VARCHAR, uid_source VARCHAR, role VARCHAR, confidence DOUBLE,
        evidence VARCHAR, evidence_ok BOOLEAN, model VARCHAR, extracted_at VARCHAR)""",
    STATUS: f"""CREATE TABLE IF NOT EXISTS {STATUS} (
        post_uid VARCHAR, domains VARCHAR, ok BOOLEAN, error VARCHAR,
        n_entities INTEGER, tokens BIGINT, latency_s DOUBLE, model VARCHAR,
        extracted_at VARCHAR)""",
    DIM: f"""CREATE OR REPLACE TABLE {DIM} (
        entity_uid VARCHAR, entity_type VARCHAR, canonical_name VARCHAR,
        uid_source VARCHAR, n_mentions BIGINT, n_posts BIGINT)""",
}


def log(msg: str) -> None:
    print(f"[18] {msg}", flush=True)


def load_gazetteer() -> dict:
    """name_key(surface form) -> gazetteer entity_uid."""
    path = DATA_DIR / "gazetteer.json"
    out = {}
    for e in json.loads(path.read_text(encoding="utf-8")):
        for sf in e.get("surface_forms", []):
            out[R.name_key(sf)] = e["entity_uid"]
    return out


def derive_uid(etype: str, name: str) -> str:
    h = hashlib.sha1(f"{etype}|{R.name_key(name)}".encode("utf-8")).hexdigest()[:12]
    return f"g_{etype.lower()}_{h}"


def extract_one(item: dict, gaz: dict) -> dict:
    uid, text, domains = item["post_uid"], item["text"], item["domains"]
    res = G.chat_json(R.entity_system_prompt(domains), R.entity_user_prompt(text),
                      max_tokens=1800, validate=lambda o: R.validate_entities(o, domains))
    allowed = R.allowed_types(domains)
    mentions = []
    if res["ok"]:
        for i, e in enumerate(res["obj"]["entities"]):
            meta = allowed[e["type"]]
            name = str(e["name"]).strip()
            canon = str(e.get("canonical") or name).strip()
            g = gaz.get(R.name_key(name)) or gaz.get(R.name_key(canon))
            mentions.append({
                "mention_id": f"{uid}#{i}", "post_uid": uid, "level": meta["level"],
                "domain": meta["domain"], "entity_type": e["type"], "name": name,
                "canonical_name": canon,
                "entity_uid": g or derive_uid(e["type"], canon),
                "uid_source": "gazetteer" if g else "derived",
                "role": str(e.get("role") or "") or None,
                "confidence": float(e.get("confidence", 0.0)),
                "evidence": e.get("evidence"),
                "evidence_ok": R.evidence_in_text(e.get("evidence") or "", text),
            })
    return {"status": {"post_uid": uid, "domains": ",".join(domains), "ok": res["ok"],
                       "error": res["error"], "n_entities": len(mentions),
                       "tokens": res["tokens"], "latency_s": res["latency_s"]},
            "mentions": mentions}


def load_work(limit, seed) -> list[dict]:
    geo = duckdb.connect(str(GEMMA_DB_PATH), read_only=True)
    try:
        routes = geo.execute(
            "SELECT post_uid, domain_1, domain_2, routed_at FROM post_route_v1 WHERE ok").df()
        done = set()
        if geo.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name=?",
                       [STATUS]).fetchone()[0]:
            done = {r[0] for r in geo.execute(f"SELECT post_uid FROM {STATUS} WHERE ok").fetchall()}
    finally:
        geo.close()
    routes = routes[~routes["post_uid"].isin(done)]
    # most recently routed first: 17 routes new posts first, so this keeps them ahead
    routes = routes.sort_values("routed_at", ascending=False)
    if limit:
        routes = routes.head(limit)
    src = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        text = dict(src.execute(
            "SELECT post_uid, text FROM post_facts WHERE post_uid IN "
            "(SELECT unnest(?))", [routes["post_uid"].tolist()]).fetchall())
    finally:
        src.close()
    text.update(R.incoming_texts([u for u in routes["post_uid"] if u not in text]))
    work = []
    for r in routes.itertuples():
        doms = [d for d in (r.domain_1, r.domain_2) if d and d != R.NONE_DOMAIN]
        work.append({"post_uid": r.post_uid, "text": text.get(r.post_uid, ""), "domains": doms})
    return [w for w in work if w["text"]]


def write(statuses: list[dict], mentions: list[dict]) -> None:
    model, now = gemma_config()["model"], datetime.now(timezone.utc).isoformat()
    con = duckdb.connect(str(GEMMA_DB_PATH))
    try:
        for name in (MENTIONS, STATUS):
            con.execute(DDL[name])
        sdf = pd.DataFrame(statuses).assign(model=model, extracted_at=now)
        con.register("_s", sdf)
        con.execute(f"INSERT INTO {STATUS} SELECT post_uid, domains, ok, error, n_entities, "
                    "tokens, latency_s, model, extracted_at FROM _s")
        con.unregister("_s")
        if mentions:
            mdf = pd.DataFrame(mentions).assign(model=model, extracted_at=now)
            con.register("_m", mdf)
            con.execute(f"INSERT INTO {MENTIONS} SELECT mention_id, post_uid, level, domain, "
                        "entity_type, name, canonical_name, entity_uid, uid_source, role, "
                        "confidence, evidence, evidence_ok, model, extracted_at FROM _m")
            con.unregister("_m")
        con.execute(DDL[DIM])
        con.execute(f"""INSERT INTO {DIM}
            SELECT entity_uid, any_value(entity_type), arg_max(canonical_name, cnt),
                   any_value(uid_source), sum(cnt), sum(posts)
            FROM (SELECT entity_uid, entity_type, canonical_name, uid_source,
                         count(*) cnt, count(DISTINCT post_uid) posts
                  FROM {MENTIONS} GROUP BY ALL)
            GROUP BY entity_uid""")
    finally:
        con.close()


def report(statuses, mentions) -> None:
    s = pd.DataFrame(statuses)
    log(f"ok rate: {100 * s['ok'].mean():.1f}%  failures: "
        f"{dict(Counter(s.loc[~s['ok'], 'error']))}")
    log(f"tokens/post mean {s['tokens'].mean():.0f}   entities/post mean {s['n_entities'].mean():.2f}")
    if mentions:
        m = pd.DataFrame(mentions)
        log(f"evidence verbatim: {100 * m['evidence_ok'].mean():.1f}%   "
            f"gazetteer-resolved: {100 * (m['uid_source'] == 'gazetteer').mean():.1f}%")
        log(f"L1/L2 split: {dict(Counter(m['level']))}")
        log(f"top types: {dict(Counter(m['entity_type']).most_common(10))}")
    log("dry run -- nothing written. Add --apply to write the three tables.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--chunk", type=int, default=500)
    args = ap.parse_args()

    cfg = gemma_config()
    log(f"endpoint host: {G.endpoint_host()}  model: {cfg['model']}  "
        f"concurrency: {cfg['concurrency']}")
    gaz = load_gazetteer()
    work = load_work(None if args.all else args.limit, args.seed)
    log(f"{len(work):,} routed posts to extract")
    if not work:
        return

    all_s, all_m = [], []
    for s in range(0, len(work), args.chunk):
        res = G.run_batch(work[s:s + args.chunk], lambda it: extract_one(it, gaz))
        statuses = [r["status"] for r in res]
        mentions = [m for r in res for m in r["mentions"]]
        log(f"  {min(s + args.chunk, len(work)):,}/{len(work):,}")
        if args.apply:
            write(statuses, mentions)
        else:
            all_s += statuses
            all_m += mentions
    if not args.apply:
        report(all_s, all_m)


if __name__ == "__main__":
    main()
