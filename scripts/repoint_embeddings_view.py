#!/usr/bin/env python3
"""Re-point analytics.duckdb's `post_embeddings` view at THIS repo's parquet folder.

WHY
---
`post_embeddings` is a VIEW over data/parquet/post_embeddings/*.parquet, stored with the ABSOLUTE path of the
machine where the database was built. After the repo (or data/) is copied to another machine or another folder
the view still points at the old path, so scripts 07, 15, 16 and the dashboard's "similar posts" fail with
"No files found that match the pattern ...". This script rewrites the view for the current location.

Safe to run any time: it does nothing when the view already points at the right folder, and it changes only
that one view (no table data is touched). It needs no .env and no production access.

    python scripts/repoint_embeddings_view.py

Stop anything that has data/analytics.duckdb open first (dashboard server / container, other pipeline stages):
DuckDB allows only one writer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "analytics.duckdb"
PQ = ROOT / "data" / "parquet" / "post_embeddings"


def main() -> int:
    if not DB.exists():
        print(f"[repoint] {DB} not found - run this from a repo that has data/analytics.duckdb")
        return 1
    files = sorted(PQ.glob("*.parquet"))
    if not files:
        print(f"[repoint] no parquet files in {PQ} - copy data/parquet/post_embeddings/ from the source machine first")
        return 1
    target = f"{PQ.resolve()}/*.parquet"
    try:
        con = duckdb.connect(str(DB))
    except duckdb.IOException as e:
        print(f"[repoint] cannot open {DB} for writing ({str(e).splitlines()[0]}).\n"
              f"          Stop the dashboard server / container and any running pipeline stage, then retry.")
        return 1
    try:
        row = con.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'post_embeddings' AND NOT internal").fetchone()
        if row and target in row[0]:
            n = con.execute("SELECT count(*) FROM post_embeddings").fetchone()[0]
            print(f"[repoint] already correct: post_embeddings -> {target}  ({n:,} rows)")
            return 0
        con.execute(f"CREATE OR REPLACE VIEW post_embeddings AS SELECT * FROM read_parquet('{target}', (union_by_name = true))")
        n = con.execute("SELECT count(*) FROM post_embeddings").fetchone()[0]
        if n == 0:
            raise SystemExit("[repoint] the view returns 0 rows - check the parquet files")
        con.execute("CHECKPOINT")
        print(f"[repoint] updated: post_embeddings -> {target}  ({n:,} rows)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
