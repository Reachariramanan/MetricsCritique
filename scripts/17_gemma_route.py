#!/usr/bin/env python3
"""Route each post to a security domain (D01-D09 / none) with the local Gemma.

First half of the NER rewrite (plan Workstream D). The routing decides which L2 entity
list the next stage (18_gemma_entities.py) asks for, and keeps the runner-up domain so
a post that straddles two domains is extracted against both -- the same top-2 idea as
the soft topic membership, applied to domains.

Output table (new, versioned; the wikineural `entity_*` tables are untouched):
  post_route_v1(post_uid, platform, lang, domain_1, bucket_1, confidence_1,
                domain_2, bucket_2, event_mode, evidence, evidence_ok,
                ok, error, tokens, latency_s, model, routed_at)

USAGE
-----
    python scripts/17_gemma_route.py                  # pilot: 200 posts, report only
    python scripts/17_gemma_route.py --limit 2000 --apply
    python scripts/17_gemma_route.py --all --apply    # full corpus, resumable

Reads `analytics.duckdb` read-only; writes only `gemma_ner.duckdb` and only with
--apply. Needs GEMMA_URL / GEMMA_MODEL / GEMMA_API_KEY in `.env` (no defaults).
Stop `UI/api_server.py` before --apply (DuckDB is single-writer).

Judge before trusting: hand-label ~150 posts stratified by domain and language and
report per-domain precision/recall and the top-2 hit rate (plan gate Workstream D-a).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import entity_routing as R  # noqa: E402
from lib import gemma_client as G  # noqa: E402
from lib.config import GEMMA_DB_PATH, gemma_config  # noqa: E402

TABLE = "post_route_v1"
DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        post_uid VARCHAR, platform VARCHAR, lang VARCHAR,
        domain_1 VARCHAR, bucket_1 VARCHAR, confidence_1 DOUBLE,
        domain_2 VARCHAR, bucket_2 VARCHAR,
        event_mode VARCHAR, evidence VARCHAR, evidence_ok BOOLEAN,
        ok BOOLEAN, error VARCHAR, tokens BIGINT, latency_s DOUBLE,
        model VARCHAR, routed_at VARCHAR
    )
"""


def log(msg: str) -> None:
    print(f"[17] {msg}", flush=True)


def route_one(item: tuple) -> dict:
    uid, platform, lang, text = item
    res = G.chat_json(R.routing_system_prompt(), R.routing_user_prompt(text),
                      max_tokens=600, validate=R.validate_route)
    row = {"post_uid": uid, "platform": platform, "lang": lang, "ok": res["ok"],
           "error": res["error"], "tokens": res["tokens"], "latency_s": res["latency_s"],
           "domain_1": None, "bucket_1": None, "confidence_1": None, "domain_2": None,
           "bucket_2": None, "event_mode": None, "evidence": None, "evidence_ok": None}
    if res["ok"]:
        o = res["obj"]
        d1, d2 = o["domain_1"], o.get("domain_2", R.NONE_DOMAIN)
        row.update(domain_1=d1, bucket_1=R.domain_bucket(d1), confidence_1=float(o["confidence_1"]),
                   domain_2=d2, bucket_2=R.domain_bucket(d2),
                   event_mode=o.get("event_mode", "none"), evidence=o.get("evidence"),
                   evidence_ok=R.evidence_in_text(o.get("evidence") or "", text))
    return row


def existing_uids() -> set[str]:
    if not GEMMA_DB_PATH.exists():
        return set()
    con = duckdb.connect(str(GEMMA_DB_PATH), read_only=True)
    try:
        if not con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name=?",
                           [TABLE]).fetchone()[0]:
            return set()
        return {r[0] for r in con.execute(f"SELECT post_uid FROM {TABLE} WHERE ok").fetchall()}
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--all", action="store_true", help="ignore --limit; whole corpus")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--since", help="only posts on/after this date (YYYY-MM-DD)")
    ap.add_argument("--newest-first", action="store_true", help="most recent posts first")
    ap.add_argument("--apply", action="store_true", help=f"write {TABLE}")
    ap.add_argument("--chunk", type=int, default=500)
    args = ap.parse_args()

    cfg = gemma_config()
    log(f"endpoint host: {G.endpoint_host()}  model: {cfg['model']}  "
        f"concurrency: {cfg['concurrency']}")

    done = existing_uids()
    items = R.select_posts(None if args.all else args.limit, args.seed, args.since, done, args.newest_first)
    log(f"{len(items):,} posts to route ({len(done):,} already routed)")
    if not items:
        return

    # Identical texts (Telegram forwards, reposts) are routed once and copied to every
    # post that carries them.
    groups: dict[str, list[tuple]] = {}
    for it in items:
        groups.setdefault(R.name_key(it[3]), []).append(it)
    log(f"{len(groups):,} distinct texts")

    all_rows: list[dict] = []
    t0 = datetime.now(timezone.utc)
    keys = list(groups)
    for s in range(0, len(keys), args.chunk):
        batch = keys[s:s + args.chunk]
        rows = G.run_batch([groups[k][0] for k in batch], route_one)
        out = []
        for k, r in zip(batch, rows):
            for uid, platform, lang, _text in groups[k]:
                out.append({**r, "post_uid": uid, "platform": platform, "lang": lang})
        log(f"  {min(s + args.chunk, len(keys)):,}/{len(keys):,} distinct texts routed")
        if args.apply:
            _write(out)
        else:
            all_rows.extend(out)

    if not args.apply:
        _report(all_rows, t0, cfg)
    else:
        log("written. Run again with the same arguments to resume.")


def _write(rows: list[dict]) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["model"] = gemma_config()["model"]
    df["routed_at"] = datetime.now(timezone.utc).isoformat()
    con = duckdb.connect(str(GEMMA_DB_PATH))
    try:
        con.execute(DDL)
        con.register("_r", df)
        con.execute(f"INSERT INTO {TABLE} SELECT post_uid, platform, lang, domain_1, bucket_1, "
                    "confidence_1, domain_2, bucket_2, event_mode, evidence, evidence_ok, ok, "
                    "error, tokens, latency_s, model, routed_at FROM _r")
        con.unregister("_r")
    finally:
        con.close()


def _report(rows: list[dict], t0: datetime, cfg: dict) -> None:
    df = pd.DataFrame(rows)
    ok = df[df["ok"]]
    log(f"ok rate: {100 * len(ok) / len(df):.1f}%  failures: "
        f"{dict(Counter(df.loc[~df['ok'], 'error']))}")
    if ok.empty:
        return
    log(f"domain_1 distribution: {dict(Counter(ok['domain_1']).most_common())}")
    log(f"has runner-up domain: {100 * (ok['domain_2'] != R.NONE_DOMAIN).mean():.1f}%   "
        f"evidence verbatim: {100 * ok['evidence_ok'].mean():.1f}%")
    log(f"tokens/post mean {ok['tokens'].mean():.0f}   latency mean {ok['latency_s'].mean():.1f}s")
    wall = (datetime.now(timezone.utc) - t0).total_seconds()
    per_post = wall / max(1, len(df))
    log(f"pilot wall {wall:.0f}s => {per_post:.2f}s/post at concurrency {cfg['concurrency']}; "
        f"full corpus (~270k posts) ~ {270_000 * per_post / 3600:.1f} h")
    log("dry run -- nothing written. Add --apply to write " + TABLE)


if __name__ == "__main__":
    main()
