#!/usr/bin/env python3
"""Complete the v4 topic layer: new posts, noise posts, and a runner-up topic.

WHY
---
`post_topic_v4` only covers posts that existed when `15_topic_clustering_v4.py` last
ran, and v4 deliberately gives HDBSCAN noise `topic_id=NULL`. Both groups reach
topic-keyed metrics as `unassigned`, so topic alerts silently ignore them. Reclustering
needs the GPU/cuML (often unavailable); assigning to the *existing* sub-topic centroids
is cheap, deterministic and runs on CPU.

It also records each post's **second-best** topic and the similarity margin, which
the soft-membership step (plan Step 1) needs and which no earlier stage kept.

HOW
---
1. Centroid per v4 sub-topic = mean of the L2-normalised bge-m3 embeddings of its
   `assign_method='hdbscan'` members.
2. A topic's acceptance threshold = the MEMBER_PCTILE-th percentile of its own members'
   cosine to the centroid, floored at MIN_SIM. A post below its best topic's threshold
   stays unassigned (`below_threshold`): it may belong to a topic that does not exist
   yet, and forcing it into the nearest one manufactures signal.
3. For EVERY post: best and second-best topic (cosine), their parents, and the margin.
   Posts already clustered by HDBSCAN keep their topic; only their runner-up is added.
4. Output goes to NEW tables in `geo_ontology.duckdb` (never overwrites v4):
   `post_topic_v4_ext`  -- one row per embedded post (effective assignment + top-2)
   `topic_coverage_v4`  -- per platform: clustered / fallback / below-threshold shares

LIMITS (stated, not hidden)
---------------------------
* Cannot discover new topics; a drifting corpus needs a real recluster (script 15).
* Centroid assignment is a weaker label than the clustering itself. Hand-judge a
  stratified sample (plan gate: ~120 posts) before it feeds metrics or alerts.
* `confidence` / `sim_1` is a cosine, not a membership probability.

USAGE
-----
    python scripts/16_assign_new_posts_topics.py            # dry run: report only
    python scripts/16_assign_new_posts_topics.py --apply    # write the two tables

Reads `analytics.duckdb` and `geo_ontology.duckdb` read-only. Writes only
`data/topic_ext_v4.duckdb` (its own file, so the dashboard server does not block it).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import (DATA_DIR, DUCKDB_PATH, GEO_DB_PATH, TOPIC_DIM_TABLE,  # noqa: E402
                        TOPIC_EXT_TABLE, TOPIC_POST_TABLE)
from lib.topic_version import TOPIC_EXT_DB_NAME  # noqa: E402

EXT_DB_PATH = DATA_DIR / TOPIC_EXT_DB_NAME

MIN_SIM = 0.45          # absolute floor on cosine to the assigned centroid
MEMBER_PCTILE = 5.0     # per-topic acceptance threshold, from its own members
MIN_TOPIC_MEMBERS = 20  # topics smaller than this are not used as targets
BATCH = 20000
# Topics never used as nearest-centroid targets: flagged incoherent by script 15, or whose
# keywords are mostly stopwords. A pure-stopword topic ('the, this, and') has a centroid that
# generic chatter sits near, so it attracted ~1/6 of a blind-judged sample of fallbacks.
JUNK_STOPWORD_SHARE = 0.5
_STOPWORDS = frozenset("""the this and that a an to of in is for it on with you i we are be was
    have has not but at as by from or they he she his her their our your my me what when who how
    all can will just so if there about out up more do no""".split())


def junk_topics(dim: pd.DataFrame) -> set:
    bad = set()
    for r in dim.itertuples():
        words = [w.strip().lower() for w in (r.terms or "").split(",") if w.strip()]
        share = sum(w in _STOPWORDS for w in words) / max(1, len(words))
        if r.coherent is False or share >= JUNK_STOPWORD_SHARE:
            bad.add(r.topic_id)
    return bad

EXT_DDL = f"""
    CREATE OR REPLACE TABLE {TOPIC_EXT_TABLE} (
        post_uid VARCHAR, platform VARCHAR,
        topic_id VARCHAR, parent_topic_id VARCHAR,
        sim_1 DOUBLE,
        topic_id_2 VARCHAR, parent_topic_id_2 VARCHAR, sim_2 DOUBLE,
        margin DOUBLE,
        assign_method VARCHAR,   -- hdbscan | nearest_centroid | below_threshold
        source VARCHAR           -- clustered | noise | new
    )
"""
COVERAGE_DDL = """
    CREATE OR REPLACE TABLE topic_coverage_v4 (
        platform VARCHAR, posts BIGINT, clustered BIGINT, fallback BIGINT,
        below_threshold BIGINT, v4_noise BIGINT, new_posts BIGINT,
        topic_coverage_pct DOUBLE
    )
"""


def log(msg: str) -> None:
    print(f"[16] {msg}", flush=True)


def _unit(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def load_embeddings() -> tuple[pd.DataFrame, np.ndarray]:
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        df = con.execute("SELECT post_uid, platform, embedding FROM post_embeddings").df()
    finally:
        con.close()
    df = df.drop_duplicates("post_uid").reset_index(drop=True)
    mat = np.vstack(df.pop("embedding").to_numpy()).astype(np.float32)
    return df, _unit(mat)


def build_centroids(pos: dict, mat: np.ndarray, members: pd.DataFrame):
    topics, cents, thr = [], [], []
    for tid, grp in members.groupby("topic_id"):
        rows = [pos[u] for u in grp["post_uid"] if u in pos]
        if len(rows) < MIN_TOPIC_MEMBERS:
            continue
        m = mat[rows]
        c = m.mean(axis=0)
        c /= (np.linalg.norm(c) or 1.0)
        topics.append(tid)
        cents.append(c)
        thr.append(max(MIN_SIM, float(np.percentile(m @ c, MEMBER_PCTILE))))
    return topics, np.vstack(cents), np.array(thr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help=f"write {TOPIC_EXT_TABLE}")
    args = ap.parse_args()

    ids, mat = load_embeddings()
    pos = {u: i for i, u in enumerate(ids["post_uid"])}
    log(f"{len(ids):,} embedded posts loaded")

    geo = duckdb.connect(str(GEO_DB_PATH), read_only=True)   # v4 tables: read-only, always
    try:
        v4 = geo.execute(
            f"SELECT post_uid, topic_id, parent_topic_id, assign_method FROM {TOPIC_POST_TABLE}"
        ).df()
        parent_of = dict(geo.execute(
            f"SELECT topic_id, parent_topic_id FROM {TOPIC_DIM_TABLE}").fetchall())

        junk = junk_topics(geo.execute(
            f"SELECT topic_id, terms, coherent FROM {TOPIC_DIM_TABLE}").df())
        log(f"{len(junk)} junk/incoherent topics excluded as fallback targets")
        members = v4[(v4["assign_method"] == "hdbscan") & v4["topic_id"].notna()
                     & ~v4["topic_id"].isin(junk)]
        topics, cents, thr = build_centroids(pos, mat, members)
        tarr = np.array(topics, dtype=object)
        parr = np.array([parent_of.get(t) for t in topics], dtype=object)
        log(f"{len(topics)} usable sub-topics (>= {MIN_TOPIC_MEMBERS} members); "
            f"threshold range {thr.min():.2f}-{thr.max():.2f}")

        # Top-2 for every post, in batches (N x K similarity matrix is small).
        n = len(ids)
        b1 = np.empty(n, dtype=int); s1 = np.empty(n, dtype=np.float32)
        b2 = np.empty(n, dtype=int); s2 = np.empty(n, dtype=np.float32)
        for s in range(0, n, BATCH):
            sims = mat[s:s + BATCH] @ cents.T
            top = np.argpartition(-sims, 1, axis=1)[:, :2]
            rows = np.arange(len(sims))[:, None]
            order = np.argsort(-sims[rows, top], axis=1)
            top = np.take_along_axis(top, order, axis=1)
            b1[s:s + BATCH], b2[s:s + BATCH] = top[:, 0], top[:, 1]
            s1[s:s + BATCH] = sims[np.arange(len(sims)), top[:, 0]]
            s2[s:s + BATCH] = sims[np.arange(len(sims)), top[:, 1]]

        # Effective assignment. HDBSCAN members keep their topic; the runner-up is the
        # best centroid that is not their own.
        v4i = v4.set_index("post_uid")
        method = np.full(n, "new", dtype=object)      # source
        own = np.full(n, None, dtype=object)
        for u, row in v4i.iterrows():
            i = pos.get(u)
            if i is None:
                continue
            if row["assign_method"] == "hdbscan" and row["topic_id"] is not None:
                method[i] = "clustered"; own[i] = row["topic_id"]
            else:
                method[i] = "noise"

        t_index = {t: j for j, t in enumerate(topics)}
        topic_1 = np.empty(n, dtype=object); sim_1 = np.empty(n, dtype=float)
        topic_2 = np.empty(n, dtype=object); sim_2 = np.empty(n, dtype=float)
        assign = np.empty(n, dtype=object)
        for i in range(n):
            if method[i] == "clustered" and own[i] in t_index:
                j = t_index[own[i]]
                sim_1[i] = float(mat[i] @ cents[j])
                topic_1[i], assign[i] = own[i], "hdbscan"
                if b1[i] == j:
                    topic_2[i], sim_2[i] = tarr[b2[i]], float(s2[i])
                else:
                    topic_2[i], sim_2[i] = tarr[b1[i]], float(s1[i])
            elif method[i] == "clustered":      # member of a topic too small to be a target
                topic_1[i], sim_1[i], assign[i] = own[i], np.nan, "hdbscan"
                topic_2[i], sim_2[i] = tarr[b1[i]], float(s1[i])
            else:
                j = b1[i]
                sim_1[i] = float(s1[i])
                ok = s1[i] >= thr[j]
                topic_1[i] = tarr[j] if ok else None
                assign[i] = "nearest_centroid" if ok else "below_threshold"
                topic_2[i], sim_2[i] = tarr[b2[i]], float(s2[i])

        par = {t: parent_of.get(t) for t in set(tarr) | {x for x in own if x}}
        out = pd.DataFrame({
            "post_uid": ids["post_uid"].to_numpy(), "platform": ids["platform"].to_numpy(),
            "topic_id": topic_1,
            "parent_topic_id": [par.get(t) if t is not None else None for t in topic_1],
            "sim_1": sim_1, "topic_id_2": topic_2,
            "parent_topic_id_2": [par.get(t) for t in topic_2],
            "sim_2": sim_2, "margin": sim_1 - sim_2,
            "assign_method": assign, "source": method,
        })

        out["_has"] = out["topic_id"].notna()
        cov = out.groupby("platform").agg(
            posts=("post_uid", "size"),
            clustered=("assign_method", lambda s: int((s == "hdbscan").sum())),
            fallback=("assign_method", lambda s: int((s == "nearest_centroid").sum())),
            below_threshold=("assign_method", lambda s: int((s == "below_threshold").sum())),
            v4_noise=("source", lambda s: int((s == "noise").sum())),
            new_posts=("source", lambda s: int((s == "new").sum())),
            has=("_has", "sum"),
        )
        cov["topic_coverage_pct"] = (100 * cov["has"] / cov["posts"]).round(1)
        cov = cov.drop(columns="has").reset_index()
        out = out.drop(columns="_has")
        log("coverage by platform:\n" + cov.to_string(index=False))
        tot = int(out["topic_id"].notna().sum())
        log(f"overall with a topic: {100 * tot / n:.1f}%  (v4 alone: "
            f"{100 * (out['assign_method'] == 'hdbscan').sum() / n:.1f}%)")
        fb = out[out["assign_method"] == "nearest_centroid"]
        log(f"fallback assignments: {len(fb):,}  median cosine "
            f"{fb['sim_1'].median():.3f}" if len(fb) else "no fallback assignments")
        amb = int((out["margin"] < 0.05).sum())
        log(f"posts with margin < 0.05 (candidates for soft weights): {amb:,}")

        if not args.apply:
            log(f"dry run -- nothing written. Re-run with --apply to write "
                f"{TOPIC_EXT_TABLE} and topic_coverage_v4.")
            return

        # Explicit DDL: ids are VARCHAR whether or not populated (a CREATE TABLE AS on
        # an all-NULL column infers INTEGER and later joins return 0 rows silently).
        wcon = duckdb.connect(str(EXT_DB_PATH))      # separate file: no lock on geo_ontology
        try:
            wcon.execute(EXT_DDL)
            wcon.register("_ext", out)
            wcon.execute(f"INSERT INTO {TOPIC_EXT_TABLE} SELECT * FROM _ext")
            wcon.unregister("_ext")
            wcon.execute(COVERAGE_DDL)
            wcon.register("_cov", cov)
            wcon.execute("INSERT INTO topic_coverage_v4 SELECT * FROM _cov")
            wcon.unregister("_cov")
            cnt = wcon.execute(f"SELECT COUNT(*) FROM {TOPIC_EXT_TABLE}").fetchone()[0]
            log(f"wrote {TOPIC_EXT_TABLE}: {cnt:,} rows; topic_coverage_v4: {len(cov)} platforms")
        finally:
            wcon.close()
    finally:
        geo.close()


if __name__ == "__main__":
    main()
