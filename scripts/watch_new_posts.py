#!/usr/bin/env python3
"""Watch production Postgres (READ-ONLY) for posts newer than anything already seen.

New posts are queued in `data/incoming_posts.duckdb` and the Gemma routing/entity stages
(`17`, `18`) process them BEFORE the backlog, so the newest content is always labelled first.

* Only SELECTs through `lib.pg.read_only_snapshot()` (REPEATABLE READ, ROLLBACK). Nothing is
  ever written to `shaktidb_social`.
* Reuses the per-platform SELECTs of `01_extract_base.PLATFORM_SQL` (so `native_id`, `text`,
  `lang`, timestamps mean exactly what they mean there) minus the heavy `embedding` column,
  wrapped with a `scraped_at > watermark` filter. `post_uid` is built with the same hash as
  `01` so a later full extract maps onto the same ids.
* The first watermark per platform is the newest `scraped_at` already in
  `analytics.duckdb.post_facts`, so everything the last extract missed counts as new.
* Writes only its own file (`incoming_posts.duckdb`): it never needs a lock on any other db.

    python scripts/watch_new_posts.py              # poll forever (every --interval s)
    python scripts/watch_new_posts.py --once       # one poll, then exit
    touch logs/watch_new_posts.stop                # clean stop
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import DATA_DIR, DUCKDB_PATH  # noqa: E402
from lib.pg import q, read_only_snapshot  # noqa: E402

INCOMING_DB = DATA_DIR / "incoming_posts.duckdb"
STOP = Path(__file__).resolve().parents[1] / "logs" / "watch_new_posts.stop"
PER_POLL_CAP = 5000          # rows per platform per poll; the watermark advances by what was read
MIN_TEXT_CHARS = 40

DDL = [
    """CREATE TABLE IF NOT EXISTS incoming_posts (
        post_uid VARCHAR PRIMARY KEY, platform VARCHAR, lang VARCHAR, text VARCHAR,
        posted_at TIMESTAMPTZ, scraped_at TIMESTAMPTZ, seen_at VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS watch_state (
        platform VARCHAR PRIMARY KEY, watermark TIMESTAMPTZ, last_poll VARCHAR, new_seen BIGINT)""",
]


def log(msg: str) -> None:
    print(f"[watch] {datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def _uid(*parts) -> str:     # identical to 01_extract_base._uid
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def _md5_text(text) -> str:  # identical to 01_extract_base._md5_text
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest() if text else ""


def load_platform_sql() -> dict:
    spec = importlib.util.spec_from_file_location(
        "extract01", Path(__file__).resolve().parent / "01_extract_base.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out = {}
    for plat, sql in m.PLATFORM_SQL.items():
        stripped = re.sub(r",\s*[^,\n]*?\bembedding\s*(\n\s*FROM)", r"\1", sql)  # also `NULL::vector AS embedding`
        if "embedding" in stripped.split("FROM")[0]:
            raise RuntimeError(f"could not strip the embedding column for {plat}")
        out[plat] = stripped
    return out


def _ts(v):
    """duckdb TIMESTAMPTZ read via VARCHAR (reading it natively needs pytz, absent from .venv)."""
    if v is None:
        return None
    d = datetime.fromisoformat(str(v).replace(" ", "T"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def initial_watermarks() -> dict:
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        return {p: _ts(v) for p, v in con.execute(
            "SELECT platform, max(scraped_at)::VARCHAR FROM post_facts GROUP BY 1").fetchall()}
    finally:
        con.close()


def _open_rw(retries: int = 20):
    for i in range(retries):
        try:
            return duckdb.connect(str(INCOMING_DB))
        except duckdb.IOException:
            time.sleep(0.5)              # a reader (API / worker) has it open for a moment
    raise RuntimeError("incoming_posts.duckdb stayed locked")


def poll(sqls: dict) -> int:
    con = _open_rw()
    try:
        for d in DDL:
            con.execute(d)
        wm = {p: _ts(v) for p, v in con.execute(
            "SELECT platform, watermark::VARCHAR FROM watch_state").fetchall()}
        if len(wm) < len(sqls):
            base = initial_watermarks()
            for p in sqls:
                wm.setdefault(p, base.get(p))
        total = 0
        with read_only_snapshot() as (_conn, cur, _snap):
            for plat, sql in sqls.items():
                mark = wm.get(plat)
                where = "WHERE scraped_at IS NOT NULL" + (" AND scraped_at > %s" if mark else "")
                query = (f"SELECT native_id, text, lang, posted_at, scraped_at, author_id FROM ({sql}) t "
                         f"{where} ORDER BY scraped_at LIMIT {PER_POLL_CAP}")
                cols, rows = q(cur, query.replace("%s", "%s"), (mark,) if mark else None)
                new = 0
                newest = mark
                for native_id, text, lang, posted, scraped, author in rows:
                    newest = scraped if newest is None or scraped > newest else newest
                    if not text or len(text.strip()) < MIN_TEXT_CHARS:
                        continue
                    uid = (_uid(plat, str(native_id)) if native_id is not None and str(native_id).strip()
                           else _uid(plat, _md5_text(text), str(author or "")))
                    con.execute("INSERT OR IGNORE INTO incoming_posts VALUES (?,?,?,?,?,?,?)",
                                [uid, plat, lang or "und", text, posted, scraped,
                                 datetime.now(timezone.utc).isoformat()])
                    new += 1
                total += new
                con.execute("""INSERT INTO watch_state VALUES (?,?,?,?)
                               ON CONFLICT (platform) DO UPDATE SET watermark=excluded.watermark,
                               last_poll=excluded.last_poll,
                               new_seen=watch_state.new_seen + ?""",
                            [plat, newest, datetime.now(timezone.utc).isoformat(), new, new])
                if rows:
                    log(f"{plat}: {len(rows):,} rows read, {new:,} queued")
        return total
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    sqls = load_platform_sql()
    log(f"watching {len(sqls)} platforms every {args.interval}s (read-only)")
    STOP.unlink(missing_ok=True)
    while True:
        try:
            n = poll(sqls)
            log(f"poll done: {n:,} new posts queued")
        except Exception as e:                       # keep watching through transient db errors
            log(f"poll failed: {type(e).__name__}: {e}")
        if args.once or STOP.exists():
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
