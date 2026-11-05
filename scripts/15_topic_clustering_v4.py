#!/usr/bin/env python3
"""Hierarchical topic clustering on GPU -- parent topics and sub-topics.

WHY THIS FILE EXISTS
--------------------
`topic_dim_v3` / `post_topic_v3` are what the product serves, and until now
nothing in `scripts/` could rebuild them -- they existed only as ad-hoc
scratchpad pickles. That is a reproducibility defect of mine, recorded in
`docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`. This script is the fix: it rebuilds the
topic layer end-to-end from `post_embeddings`, and it writes **v4** rather than
overwriting v3, so the serving tables are never left in a half-built state and
the two can be compared.

WHAT CHANGED FROM v3, AND WHY (all four are measured, not asserted)
-------------------------------------------------------------------
1. **`MIN_CLUSTER_SIZE` is one explicit constant.** `09_ontology_v2_cluster_fix.py`
   declared it "a FIXED ABSOLUTE CONSTANT ... does not scale with N" and then
   overrode it from a hardcoded ladder; `topic_dim_v2` shows the value actually
   used was 292, 19x the declared 15. The N-coupled coarsening failure mode was
   laundered through the ladder rather than removed. Here the value is chosen by
   a measured stability sweep (see below) and stated once.

2. **Silhouette is computed in the ORIGINAL 1024-d cosine space**, never in UMAP
   space. UMAP is explicitly optimised to exaggerate separation, so scoring there
   inflates. Measured: v3 stored 0.395; the same clustering scored in the real
   embedding space lands at 0.04-0.09. The honest number is the small one.

3. **Stability is measured, not left null.** `reports/ontology_summary.json` has
   `"stability_ari": null` for the umap_hdbscan path. This runs N_SEEDS UMAP+HDBSCAN
   fits and reports pairwise adjusted Rand index. This is what selected
   MIN_CLUSTER_SIZE:

       mcs   k across 5 seeds            mean ARI   min ARI
        15   [280,290,291,276,287]          0.552     0.534
        20   [215,234,212,214,223]          0.578     0.563
        25   [166,169,158,157,170]          0.589     0.557
        30   [136,133,126,131,141]          0.595     0.556   <- chosen
        40   [109,107, 94,102,115]          0.575     0.525
        60   [ 73, 71,  8, 70,  8]          0.288     0.017   <- CLIFF

   mcs=60 is not "coarser", it is *broken*: two of five seeds collapse to 8
   clusters and pairwise ARI reaches 0.017, which is chance agreement. The v3
   production clustering (k=33) sits in that coarse region. Anything above ~40
   here is a landmine, and that is exactly the kind of thing a hardcoded ladder
   walks into silently.

4. **Hierarchy is built by agglomerating cluster centroids, not by a second
   HDBSCAN pass.** A coarse HDBSCAN run would land on the cliff above. Ward
   linkage over the fine clusters' centroids is deterministic given the clusters,
   so the parent level adds no new instability.

GPU
---
cuML (RAPIDS) UMAP + HDBSCAN. Measured on an L40S: UMAP 21,228x1024 -> 10d in
1.3 s, HDBSCAN in 0.1 s. The whole multi-seed stability sweep costs seconds, which
is why it is run every time rather than being a thing we promise to do later.
Falls back to CPU umap-learn / hdbscan if cuML is unavailable.

Run:
    .venv/bin/python scripts/15_topic_clustering_v4.py            # full build
    .venv/bin/python scripts/15_topic_clustering_v4.py --sweep    # sweep only, no write
    .venv/bin/python scripts/15_topic_clustering_v4.py --seeds 5  # stability seeds

Versioned output / smoke-test flags:
    --out-version v5   write topic_dim_v5 / topic_parent_v5 / post_topic_v5 /
                       topic_noise_v5 / topic_code_crosswalk_v5 (default v4 keeps the
                       original names, incl. the unsuffixed topic_code_crosswalk)
    --out-db PATH      write there instead of data/geo_ontology.duckdb (event_codes is
                       still READ from data/geo_ontology.duckdb, read-only)
    --limit N          seeded random subset of embedded posts (smoke tests)
    --dry-run          compute + print everything (per-platform noise, k, silhouette,
                       ARI spread, per-stage timing, peak RSS); write NOTHING
    --umap-fit-n N     fit UMAP on a seeded subsample of N posts and .transform() the
                       rest (CPU memory/time escape hatch; default = fit on all)

Stability / primary seed (review items #16, #17): the N seeds are all fitted, the
pairwise ARI matrix is printed as mean/min/max, and the PRIMARY seed is the one with
the highest mean ARI to the others (ties -> lowest seed), not a hardcoded 42. Its
fit is reused (no extra UMAP). `primary_seed` and the ARI spread are stored in
topic_dim_<v>; per-platform noise share is stored in topic_noise_<v>.
"""
from __future__ import annotations

import argparse
import json
import re
import resource
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC_DB = ROOT / "data" / "analytics.duckdb"
OUT_DB = ROOT / "data" / "geo_ontology.duckdb"
# event_codes (for coded_share / crosswalk) is always READ from here, read-only,
# even when --out-db points elsewhere.
EVENTS_DB = ROOT / "data" / "geo_ontology.duckdb"
REPORT = ROOT / "reports" / "ontology_v4_summary.json"

# ---- parameters. Every one of these is a stated choice, not a ladder. -------
UMAP_COMPONENTS = 10
UMAP_NEIGHBORS = 15
UMAP_MIN_DIST = 0.0
UMAP_METRIC = "cosine"

# Selected by the stability sweep in the module docstring: highest mean pairwise
# ARI (0.595) with the cliff at 60 comfortably avoided. NOT scaled by N, and not
# overridden anywhere below -- if you want a different granularity, change this
# number and re-run --sweep to see what it costs.
MIN_CLUSTER_SIZE = 30
MIN_SAMPLES = 5

N_SEEDS = 5           # stability seeds; ARI is reported across all pairs
# Primary seed is now CHOSEN (highest mean ARI to the other seeds). 42 is only the
# seed the original v4 build happened to use; retained for the --sweep default seed
# list compatibility documentation, not used for selection.
PRIMARY_SEED = 42

# Parent level: agglomerate fine-cluster centroids. Chosen so parents stay
# human-scannable while each holds a handful of sub-topics.
N_PARENTS = 24

# A cluster is flagged INCOHERENT if it is not a real grouping: one near-identical
# text repeated, or too small to judge.
#
# MAX_AUTHOR_SHARE is 0.90, not the 0.60 first tried. Measured across the 130
# clusters, top-author share has median 0.565 and p90 0.776 -- this corpus is
# author-concentrated by nature (the metrics critique measured 7.28 posts/author
# in maritime_security, with three accounts dominating). A 0.60 cut therefore
# flagged 59 of 130 clusters, including h039 "geran/нпз/дронов" -- 975 posts of
# real Russian drone/refinery-strike reporting whose only sin is that one outlet
# files most of it. Encoding the corpus norm as an anomaly is not a quality gate.
# 0.90 flags genuine single-source clusters against a median of 0.565.
MAX_AUTHOR_SHARE = 0.90
MAX_DUP_TEXT_SHARE = 0.35
MIN_COHERENT_SIZE = 20

# Separate and deliberately NOT folded into `coherent`: a cluster can be a
# perfectly real grouping that our geopolitical taxonomy simply does not cover.
# Measured: five ransomware-victim clusters (~470 posts) and a Reddit
# moderation-boilerplate cluster receive a CAMEO theme code on ~0% of their
# posts, against a corpus median of 0.287. That is an objective disqualification
# for the artifact clusters v3 had to annotate by hand -- but calling them
# "incoherent" would be wrong. They cohere; they are off-topic.
MIN_CODED_SHARE = 0.05

LABEL_TERMS = 6
SIL_SAMPLE = 5000

# Columns whose SQL type must be declared, never inferred. `CREATE TABLE AS
# SELECT *` types each column from what the frame happens to hold, and an
# all-NULL column comes out INTEGER. That already bit us once: `event_codes`
# ended up with a VARCHAR `code` and an INTEGER `parent_code`, so '05' became 5
# and the zero-pad was lost (spec §9.8).
#
# Here the same inference typed `topic_parent_v4.parent_topic_id` INTEGER -- it
# is NULL for every row, because a parent has no parent -- while the identical
# column is VARCHAR in the other two tables. Nothing was corrupted, but the
# failure mode is worse than a crash: DuckDB resolves
#   topic_dim_v4 d JOIN topic_parent_v4 p USING (parent_topic_id)
# to a common type, matches nothing, and returns **0 rows silently**. A join
# that looks right and answers wrong is exactly the trap the explicit-DDL rule
# exists to prevent, so ids are pinned by name whether or not they are populated.
ID_TYPES = {
    "topic_id": "VARCHAR", "parent_topic_id": "VARCHAR", "post_uid": "VARCHAR",
    "cluster_id": "BIGINT", "parent_cluster_id": "BIGINT",
    "dominant_code": "VARCHAR", "medoid_post_uid": "VARCHAR",
    # stability columns are NULL when only one seed is run -- pin them so an
    # all-NULL column is DOUBLE/BIGINT, not INTEGER.
    "primary_seed": "BIGINT", "n_seeds": "BIGINT",
    "stability_mean_ari": "DOUBLE", "stability_min_ari": "DOUBLE",
    "stability_max_ari": "DOUBLE", "primary_seed_mean_ari": "DOUBLE",
}

# Explicit DDL for the noise table (review item #16). Not routed through the
# CTAS/ID_TYPES path because it has no id columns and every type is known.
NOISE_DDL = ("CREATE OR REPLACE TABLE {name} (platform VARCHAR, posts BIGINT, "
             "noise BIGINT, noise_pct DOUBLE)")


def table_names(v: str) -> dict:
    """Output table names for version `v`.

    `topic_code_crosswalk` is keyed by topic_id ("h012"), and h### ids are re-minted
    by every clustering run, so its CONTENT is version-specific even though the v4
    build never suffixed it. Writing it unsuffixed from a v5 run would silently
    replace v4's crosswalk with rows whose topic_ids mean something else. So for any
    version other than the legacy "v4" it is written as topic_code_crosswalk_<v>;
    v4 keeps the original name for backward compatibility.
    """
    return {
        "dim": f"topic_dim_{v}", "parent": f"topic_parent_{v}",
        "post": f"post_topic_{v}", "noise": f"topic_noise_{v}",
        "xw": "topic_code_crosswalk" if v == "v4" else f"topic_code_crosswalk_{v}",
    }


class Stages:
    """Wall-time per pipeline stage (summed over repeats) + peak RSS."""

    def __init__(self):
        self.t: dict[str, float] = {}
        self.n: dict[str, int] = {}

    def run(self, name: str):
        stages = self

        class _Ctx:
            def __enter__(self_):
                self_.t0 = time.time()

            def __exit__(self_, *exc):
                stages.t[name] = stages.t.get(name, 0.0) + time.time() - self_.t0
                stages.n[name] = stages.n.get(name, 0) + 1
        return _Ctx()

    def report(self) -> dict:
        return {k: {"seconds": round(v, 2), "calls": self.n[k]} for k, v in self.t.items()}

    def print(self) -> None:
        log("stage timings (wall seconds):")
        for k, v in self.t.items():
            log(f"  {k:<26} {v:>9.1f}s  x{self.n[k]}")


def peak_rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 ** 2  # KB -> GB


def _projection(payload: pd.DataFrame) -> str:
    """SELECT list that casts identifier columns to their declared type."""
    return ", ".join(
        f'CAST("{c}" AS {ID_TYPES[c]}) AS "{c}"' if c in ID_TYPES else f'"{c}"'
        for c in payload.columns)


def log(msg: str) -> None:
    print(f"[15] {msg}", flush=True)


# ============================================================================
# GPU / CPU backend
# ============================================================================
def _np(a) -> np.ndarray:
    return np.asarray(a.get() if hasattr(a, "get") else a)


def _fit_transform(make, X: np.ndarray, seed: int, fit_n: int | None,
                   chunk: int = 20000) -> np.ndarray:
    """UMAP embed X. With `fit_n`, fit on a seeded subsample and .transform() the
    rest, so a 251k-post run does not pay a full-corpus fit per seed. The
    transformed points are placed into the subsample's manifold (they do not
    reshape it), which is the accepted cost of this option."""
    n = len(X)
    if not fit_n or fit_n >= n:
        return _np(make(seed).fit_transform(X)).astype(np.float32)
    rng = np.random.default_rng(seed)
    fit_idx = np.sort(rng.choice(n, size=fit_n, replace=False))
    rest = np.setdiff1d(np.arange(n), fit_idx, assume_unique=True)
    m = make(seed)
    E = np.empty((n, UMAP_COMPONENTS), dtype=np.float32)
    E[fit_idx] = _np(m.fit_transform(X[fit_idx]))
    for a in range(0, len(rest), chunk):
        ix = rest[a:a + chunk]
        E[ix] = _np(m.transform(X[ix]))
    return E


def get_backend(umap_fit_n: int | None = None) -> tuple:
    """(umap_fn, hdbscan_fn, name). cuML if importable, else CPU."""
    try:
        from cuml.cluster import HDBSCAN as cuHDBSCAN
        from cuml.manifold import UMAP as cuUMAP

        def make(seed):
            return cuUMAP(n_components=UMAP_COMPONENTS, n_neighbors=UMAP_NEIGHBORS,
                          min_dist=UMAP_MIN_DIST, metric=UMAP_METRIC, random_state=seed)

        def umap_fn(X, seed):
            return _fit_transform(make, X, seed, umap_fit_n)

        def hdbscan_fn(E, mcs, ms):
            return _np(cuHDBSCAN(min_cluster_size=mcs, min_samples=ms).fit_predict(E))

        return umap_fn, hdbscan_fn, "cuml_gpu"
    except ImportError:
        import hdbscan as cpu_hdbscan
        import umap as cpu_umap

        def make(seed):
            return cpu_umap.UMAP(n_components=UMAP_COMPONENTS, n_neighbors=UMAP_NEIGHBORS,
                                 min_dist=UMAP_MIN_DIST, metric=UMAP_METRIC,
                                 random_state=seed)

        def umap_fn(X, seed):
            return _fit_transform(make, X, seed, umap_fit_n)

        def hdbscan_fn(E, mcs, ms):
            return cpu_hdbscan.HDBSCAN(min_cluster_size=mcs, min_samples=ms).fit_predict(E)

        return umap_fn, hdbscan_fn, "cpu_umap_hdbscan"


# ============================================================================
# Data
# ============================================================================
def load_posts(limit: int | None = None, seed: int = 0) -> pd.DataFrame:
    """Embedded posts. `limit` = deterministic pseudo-random subset: posts are
    ordered by hash(post_uid, seed), so the subset is reproducible and unbiased by
    insertion order / platform."""
    con = duckdb.connect(str(SRC_DB), read_only=True)
    sel = ""
    if limit:
        sel = (f"WHERE e.post_uid IN (SELECT e2.post_uid FROM post_embeddings e2 "
               f"JOIN post_facts p2 ON p2.post_uid = e2.post_uid "
               f"ORDER BY hash(e2.post_uid || '|{int(seed)}'), e2.post_uid LIMIT {int(limit)})")
    df = con.execute(f"""
        SELECT e.post_uid, e.embedding, p.text, p.lang, p.author_uid, p.platform
        FROM post_embeddings e
        JOIN post_facts p ON p.post_uid = e.post_uid
        {sel}
    """).fetchdf()
    con.close()
    log(f"posts with embeddings: {len(df):,}" + (f" (--limit {limit:,})" if limit else ""))
    return df


def unit_norm(df: pd.DataFrame) -> np.ndarray:
    X = np.vstack(df["embedding"].apply(np.array).values).astype(np.float32)
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return X / n


# ============================================================================
# Quality: measured in the real space, across real seeds
# ============================================================================
def cosine_silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    """Silhouette in the ORIGINAL 1024-d cosine space.

    Deliberately NOT in UMAP space: UMAP optimises for visual separation, so a
    silhouette computed there measures the projection, not the clustering. v3
    stored 0.395 from UMAP space; the honest figure for the same data is an order
    of magnitude smaller.
    """
    from sklearn.metrics import silhouette_score
    m = labels >= 0
    k = len(set(labels[m]))
    if k < 2 or m.sum() < 10:
        return float("nan")
    return float(silhouette_score(X[m], labels[m], metric="cosine",
                                  sample_size=min(SIL_SAMPLE, int(m.sum())),
                                  random_state=0))


def stability(X, umap_fn, hdbscan_fn, mcs, ms, seeds, stages: Stages | None = None):
    """Fit UMAP+HDBSCAN once per seed and compare all pairs.

    Returns (summary, runs). `runs[seed] = (E, labels)` so the caller can reuse the
    winning fit instead of refitting. The primary seed is the one with the highest
    mean ARI to the others (ties -> lowest seed); with <2 seeds there is nothing to
    compare, so the first seed is used and the ARI fields are None.
    """
    from sklearn.metrics import adjusted_rand_score
    st = stages or Stages()
    runs, ks = {}, []
    for s in seeds:
        with st.run("umap"):
            E = umap_fn(X, s)
        with st.run("hdbscan"):
            lab = hdbscan_fn(E, mcs, ms)
        runs[s] = (E, lab)
        ks.append(int(len(set(lab)) - (1 if -1 in lab else 0)))
        log(f"  seed {s}: k={ks[-1]} noise={100*float((lab == -1).mean()):.1f}% "
            f"(umap {st.t['umap']:.0f}s cum, hdbscan {st.t['hdbscan']:.0f}s cum)")
    pair = {}
    with st.run("ari_pairs"):
        for a in range(len(seeds)):
            for b in range(a + 1, len(seeds)):
                pair[(a, b)] = float(adjusted_rand_score(runs[seeds[a]][1], runs[seeds[b]][1]))
    aris = list(pair.values())
    per_seed_mean = {}
    if aris:
        for a, sd in enumerate(seeds):
            v = [pair[tuple(sorted((a, b)))] for b in range(len(seeds)) if b != a]
            per_seed_mean[sd] = float(np.mean(v))
        primary = max(seeds, key=lambda sd: (per_seed_mean[sd], -sd))
    else:
        primary = seeds[0]
    summary = {
        "k_per_seed": ks, "seeds": list(seeds), "n_seeds": len(seeds),
        "mean_ari": float(np.mean(aris)) if aris else None,
        "min_ari": float(np.min(aris)) if aris else None,
        "max_ari": float(np.max(aris)) if aris else None,
        "pairwise_ari": {f"{seeds[a]}-{seeds[b]}": round(v, 4) for (a, b), v in pair.items()},
        "per_seed_mean_ari": {str(k): round(v, 4) for k, v in per_seed_mean.items()},
        "primary_seed": int(primary),
        "primary_seed_mean_ari": per_seed_mean.get(primary),
    }
    return summary, runs


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _nn(x):
    """None for None/NaN, else rounded float (keeps NULL, never 0)."""
    return None if x is None or x != x else round(float(x), 4)


def noise_by_platform(df: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    g = (pd.DataFrame({"platform": df["platform"].fillna("unknown").to_numpy(),
                       "noise": (labels == -1).astype(int)})
         .groupby("platform")["noise"].agg(posts="size", noise="sum").reset_index())
    g["noise_pct"] = (100.0 * g["noise"] / g["posts"]).round(2)
    return g.sort_values("posts", ascending=False).reset_index(drop=True).astype(
        {"posts": "int64", "noise": "int64"})


# ============================================================================
# Labelling -- multilingual by construction
# ============================================================================
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
# Deliberately NOT sklearn's stop_words="english". The corpus spans 6 scripts;
# an English stoplist plus English regexes is why a v2 cluster ended up named
# "бпла". Instead: drop terms that are common ACROSS clusters, which is
# language-agnostic and adapts to whatever the corpus actually contains.
_URL = re.compile(r"https?://\S+|www\.\S+")


def tokenise(text: str) -> list[str]:
    return _TOKEN.findall(_URL.sub(" ", str(text).lower()))


def label_clusters(df: pd.DataFrame, labels: np.ndarray, X: np.ndarray) -> dict:
    """c-TF-IDF style terms per cluster, plus dominant language and a medoid.

    Distinctiveness is `share_in_cluster / share_across_corpus`, so a term that
    is frequent everywhere cannot win regardless of which language it is in.
    """
    out = {}
    doc_freq: Counter = Counter()
    per_cluster: dict[int, Counter] = {}
    for cid in sorted(set(labels)):
        if cid < 0:
            continue
        idx = np.where(labels == cid)[0]
        c: Counter = Counter()
        for i in idx:
            c.update(set(tokenise(df["text"].iloc[i])))
        per_cluster[cid] = c
        doc_freq.update(c.keys())

    n_clusters = max(1, len(per_cluster))
    for cid, c in per_cluster.items():
        idx = np.where(labels == cid)[0]
        n = len(idx)
        scored = []
        for term, cnt in c.items():
            if cnt < max(2, 0.05 * n):
                continue
            spread = doc_freq[term] / n_clusters      # in how many clusters it appears
            scored.append(((cnt / n) / (spread + 1e-9), term, cnt / n))
        scored.sort(reverse=True)
        terms = [t for _s, t, _f in scored[:LABEL_TERMS]]

        langs = Counter(df["lang"].iloc[idx].fillna("und"))
        # medoid = post closest to the cluster centroid; a real example beats a
        # bag of keywords when a human has to judge whether the cluster is real.
        cen = X[idx].mean(axis=0)
        cen /= (np.linalg.norm(cen) or 1.0)
        medoid = idx[int(np.argmax(X[idx] @ cen))]

        out[cid] = {
            "terms": terms,
            "name": " / ".join(terms[:3]) if terms else f"cluster {cid}",
            "langs": langs.most_common(3),
            "dominant_lang": langs.most_common(1)[0][0] if langs else "und",
            "medoid_post_uid": df["post_uid"].iloc[medoid],
            "medoid_text": str(df["text"].iloc[medoid])[:240].replace("\n", " "),
        }
    return out


def coherence_flags(df: pd.DataFrame, labels: np.ndarray,
                    coded: set | None = None) -> dict:
    """Flag artifact clusters objectively instead of annotating them by hand.

    Two INDEPENDENT flags, because they mean different things:
      coherent            -- is this a real grouping at all?
      off_topic_geopolitical -- is it a real grouping we simply do not code?
    """
    flags = {}
    for cid in sorted(set(labels)):
        if cid < 0:
            continue
        idx = np.where(labels == cid)[0]
        n = len(idx)
        auth = Counter(df["author_uid"].iloc[idx].fillna("?"))
        top_auth = auth.most_common(1)[0][1] / n if auth else 0.0
        txt = Counter(str(t)[:120] for t in df["text"].iloc[idx])
        top_dup = txt.most_common(1)[0][1] / n if txt else 0.0
        reasons = []
        if n < MIN_COHERENT_SIZE:
            reasons.append(f"below MIN_COHERENT_SIZE ({n} < {MIN_COHERENT_SIZE})")
        if top_auth > MAX_AUTHOR_SHARE:
            reasons.append(f"one author is {top_auth:.0%} of the cluster "
                           f"(threshold {MAX_AUTHOR_SHARE:.0%}; corpus median ~0.57)")
        if top_dup > MAX_DUP_TEXT_SHARE:
            reasons.append(f"one near-identical text is {top_dup:.0%}")

        coded_share = (len({u for u in df["post_uid"].iloc[idx]} & coded) / n
                       if coded else float("nan"))
        off_topic = bool(coded == coded and coded and coded_share < MIN_CODED_SHARE)

        flags[cid] = {"coherent": not reasons, "reason": "; ".join(reasons) or None,
                      "top_author_share": round(top_auth, 3),
                      "top_dup_share": round(top_dup, 3),
                      "coded_share": None if coded_share != coded_share else round(coded_share, 3),
                      "off_topic_geopolitical": off_topic}
    return flags


# ============================================================================
# Hierarchy
# ============================================================================
def build_parents(X: np.ndarray, labels: np.ndarray, n_parents: int) -> dict:
    """Agglomerate fine-cluster centroids into parent topics.

    Ward over L2-normalised centroids. Deterministic given the clusters, so the
    parent level introduces no instability of its own -- which a second, coarser
    HDBSCAN pass certainly would (measured: mcs=60 gives min pairwise ARI 0.017).
    """
    from sklearn.cluster import AgglomerativeClustering
    cids = sorted(c for c in set(labels) if c >= 0)
    if len(cids) < 2:
        return {c: 0 for c in cids}
    cen = np.vstack([X[labels == c].mean(axis=0) for c in cids])
    cen /= np.linalg.norm(cen, axis=1, keepdims=True).clip(min=1e-9)
    k = min(n_parents, len(cids))
    par = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(cen)
    return dict(zip(cids, (int(p) for p in par)))


def crosswalk(con, post_topic: pd.DataFrame) -> pd.DataFrame:
    """topic x CAMEO parent code, with concentration and entropy.

    Validates both directions: a topic spanning many codes is incoherent, and a
    code with no topic is under-served by the corpus.
    """
    if not con.execute("SELECT count(*) FROM duckdb_tables() "
                       "WHERE table_name='event_codes'").fetchone()[0]:
        return pd.DataFrame()
    ec = con.execute("SELECT post_uid, code_key FROM event_codes "
                     "WHERE axis='theme' AND level=1").fetchdf()
    if not len(ec):
        return pd.DataFrame()
    j = post_topic.merge(ec, on="post_uid", how="inner")
    rows = []
    for tid, g in j.groupby("topic_id"):
        cnt = g["code_key"].value_counts()
        p = (cnt / cnt.sum()).to_numpy()
        ent = float(-(p * np.log(p + 1e-12)).sum())
        rows.append({
            "topic_id": tid, "n_posts_coded": int(cnt.sum()),
            "dominant_code": cnt.index[0], "dominant_share": float(p[0]),
            "n_codes": int(len(cnt)),
            "entropy": round(ent, 4),
            "entropy_norm": round(ent / np.log(len(cnt)), 4) if len(cnt) > 1 else 0.0,
        })
    return pd.DataFrame(rows)


# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true",
                    help="run the stability sweep and exit without writing")
    ap.add_argument("--seeds", type=int, default=N_SEEDS)
    ap.add_argument("--min-cluster-size", type=int, default=MIN_CLUSTER_SIZE)
    ap.add_argument("--parents", type=int, default=N_PARENTS)
    ap.add_argument("--out-version", default="v4",
                    help="suffix for output tables: topic_dim_<v>, topic_parent_<v>, "
                         "post_topic_<v>, topic_noise_<v>, topic_code_crosswalk_<v> "
                         "(v4 keeps the legacy unsuffixed crosswalk name)")
    ap.add_argument("--out-db", default=str(OUT_DB),
                    help="DuckDB file to write (default: data/geo_ontology.duckdb)")
    ap.add_argument("--limit", type=int, default=None,
                    help="seeded random subset of embedded posts (smoke tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print everything, write nothing")
    ap.add_argument("--umap-fit-n", type=int, default=None,
                    help="fit UMAP on a seeded subsample of N posts and transform the "
                         "rest (default: fit on all)")
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.out_version):
        ap.error("--out-version must match [A-Za-z0-9_]+ (it is spliced into table names)")
    T = table_names(args.out_version)
    out_db = Path(args.out_db)

    t0 = time.time()
    stages = Stages()
    umap_fn, hdbscan_fn, backend = get_backend(args.umap_fit_n)
    log(f"backend: {backend}   out_version={args.out_version}   "
        f"{'DRY-RUN (no writes)' if args.dry_run else 'out_db=' + str(out_db)}")
    if args.umap_fit_n:
        log(f"UMAP fit on {args.umap_fit_n:,} posts per seed, transform the rest")

    with stages.run("load_posts"):
        df = load_posts(args.limit)
    with stages.run("unit_norm"):
        X = unit_norm(df)
    log(f"embeddings: {X.shape}   peak RSS so far {peak_rss_gb():.1f} GB")

    seeds = list(range(args.seeds))
    if args.sweep:
        log("stability sweep (no write)")
        print(f"\n{'mcs':>5} {'k per seed':<32} {'mean ARI':>9} {'min ARI':>8} "
              f"{'max ARI':>8} {'prim':>5} {'sil_1024d':>10}")
        for mcs in [15, 20, 25, 30, 40, 60]:
            st, runs = stability(X, umap_fn, hdbscan_fn, mcs, MIN_SAMPLES, seeds, stages)
            lab = runs[st["primary_seed"]][1]
            with stages.run("silhouette"):
                sil = cosine_silhouette(X, lab)
            print(f"{mcs:>5} {str(st['k_per_seed']):<32} {_fmt(st['mean_ari']):>9} "
                  f"{_fmt(st['min_ari']):>8} {_fmt(st['max_ari']):>8} "
                  f"{st['primary_seed']:>5} {sil:>10.3f}")
        stages.print()
        return 0

    mcs = args.min_cluster_size
    log(f"stability across {len(seeds)} seeds {seeds} at min_cluster_size={mcs} ...")
    st, runs = stability(X, umap_fn, hdbscan_fn, mcs, MIN_SAMPLES, seeds, stages)
    log(f"  k per seed {st['k_per_seed']}")
    log(f"  pairwise ARI over {len(st['pairwise_ari'])} pairs: mean {_fmt(st['mean_ari'])}  "
        f"min {_fmt(st['min_ari'])}  max {_fmt(st['max_ari'])}")
    if st["per_seed_mean_ari"]:
        log(f"  per-seed mean ARI to others: {st['per_seed_mean_ari']}")
    log(f"  primary seed = {st['primary_seed']} (highest mean ARI to the others; "
        f"not hardcoded)")
    if st["min_ari"] is not None and st["min_ari"] < 0.3:
        log("  WARNING: min pairwise ARI < 0.3 -- this clustering is not reproducible "
            "across seeds; treat every topic below as provisional")
    if st["min_ari"] is None:
        log("  NOTE: <2 seeds -- stability NOT measured (ARI fields are NULL)")
    primary = st["primary_seed"]

    E, labels = runs[primary]
    del runs
    k = int(len(set(labels)) - (1 if -1 in labels else 0))
    noise = int((labels == -1).sum())
    with stages.run("silhouette"):
        sil = cosine_silhouette(X, labels)
    log(f"clusters: {k}  noise: {noise:,} ({100*noise/len(labels):.1f}%)  "
        f"silhouette(cosine, 1024d): {sil:.3f}")
    noise_df = noise_by_platform(df, labels)
    log("noise share by platform:")
    for r in noise_df.itertuples():
        log(f"  {r.platform:<14} posts={r.posts:>8,}  noise={r.noise:>8,}  {r.noise_pct:>6.2f}%")

    with stages.run("parents"):
        parents = build_parents(X, labels, args.parents)
    n_par = len(set(parents.values()))
    log(f"parent topics: {n_par} (agglomerated from {k} sub-topics)")

    with stages.run("label_clusters"):
        labs = label_clusters(df, labels, X)
    # event_codes is READ-ONLY from EVENTS_DB regardless of --out-db.
    coded = set()
    xw_src = None
    if EVENTS_DB.exists():
        xw_src = duckdb.connect(str(EVENTS_DB), read_only=True)
        if xw_src.execute("SELECT count(*) FROM duckdb_tables() "
                          "WHERE table_name='event_codes'").fetchone()[0]:
            coded = {r[0] for r in xw_src.execute(
                "SELECT DISTINCT post_uid FROM event_codes WHERE axis='theme'").fetchall()}
    with stages.run("coherence_flags"):
        flags = coherence_flags(df, labels, coded)
    n_coh = sum(1 for f in flags.values() if f["coherent"])
    n_off = sum(1 for f in flags.values() if f["off_topic_geopolitical"])
    log(f"coherent: {n_coh}/{k}   off-topic (no geopolitical code): {n_off}/{k}")

    run_at = datetime.now(timezone.utc).isoformat()
    dim_rows, post_rows = [], []
    with stages.run("build_rows"):
        for cid in sorted(c for c in set(labels) if c >= 0):
            idx = np.where(labels == cid)[0]
            L, F = labs[cid], flags[cid]
            pid = parents[cid]
            dim_rows.append({
                "topic_id": f"h{cid:03d}", "cluster_id": int(cid),
                "parent_topic_id": f"p{pid:02d}", "parent_cluster_id": int(pid),
                "level": 2,
                "name": L["name"], "terms": ", ".join(L["terms"]),
                "dominant_lang": L["dominant_lang"],
                "langs": json.dumps(L["langs"], ensure_ascii=False),
                "medoid_post_uid": L["medoid_post_uid"], "medoid_text": L["medoid_text"],
                "coherent": F["coherent"], "incoherent_reason": F["reason"],
                "off_topic_geopolitical": F["off_topic_geopolitical"],
                "coded_share": F["coded_share"],
                "top_author_share": F["top_author_share"], "top_dup_share": F["top_dup_share"],
                "size": int(len(idx)),
                "method": f"{backend}|umap{UMAP_COMPONENTS}|mcs{mcs}|ms{MIN_SAMPLES}"
                          + (f"|fitn{args.umap_fit_n}" if args.umap_fit_n else ""),
                "n_clusters_total": k, "silhouette_cosine_1024d": round(sil, 4),
                "stability_mean_ari": _nn(st["mean_ari"]),
                "stability_min_ari": _nn(st["min_ari"]),
                "stability_max_ari": _nn(st["max_ari"]),
                "n_seeds": int(st["n_seeds"]),
                "primary_seed": int(primary),
                "primary_seed_mean_ari": _nn(st["primary_seed_mean_ari"]),
                "run_at": run_at,
            })
            # Unit centroid computed once per cluster; recomputing it per post was
            # O(N * cluster size) for an identical value.
            cen = X[idx].mean(axis=0)
            cen /= (np.linalg.norm(cen) or 1.0)
            for i in idx:
                sim = float(X[i] @ cen)
                post_rows.append({"post_uid": df["post_uid"].iloc[i],
                                  "topic_id": f"h{cid:03d}", "cluster_id": int(cid),
                                  "parent_topic_id": f"p{pid:02d}",
                                  "confidence": round(sim, 4), "assign_method": "hdbscan"})
        for i in np.where(labels == -1)[0]:
            post_rows.append({"post_uid": df["post_uid"].iloc[i], "topic_id": None,
                              "cluster_id": -1, "parent_topic_id": None,
                              "confidence": None, "assign_method": "noise"})

        topic_dim = pd.DataFrame(dim_rows)
        post_topic = pd.DataFrame(post_rows)

        # parent dimension: named from its children's most distinctive terms
        par_rows = []
        for pid in sorted(set(parents.values())):
            kids = topic_dim[topic_dim["parent_cluster_id"] == pid]
            terms = Counter()
            for t in kids["terms"]:
                terms.update(t.split(", "))
            par_rows.append({
                "topic_id": f"p{pid:02d}", "cluster_id": int(pid), "parent_topic_id": None,
                "parent_cluster_id": None, "level": 1,
                "name": " / ".join([t for t, _ in terms.most_common(3)]),
                "terms": ", ".join(t for t, _ in terms.most_common(LABEL_TERMS)),
                "n_subtopics": int(len(kids)), "size": int(kids["size"].sum()),
                "coherent": bool(kids["coherent"].any()),
                "run_at": run_at,
            })
        parent_dim = pd.DataFrame(par_rows)

    with stages.run("crosswalk"):
        xw = (crosswalk(xw_src, post_topic[post_topic["topic_id"].notna()])
              if xw_src is not None else pd.DataFrame())
    if xw_src is not None:
        xw_src.close()

    summary = {
        "run_at": run_at, "backend": backend, "out_version": args.out_version,
        "n_posts": int(len(df)), "limit": args.limit, "umap_fit_n": args.umap_fit_n,
        "n_subtopics": k, "n_parents": n_par,
        "noise": noise, "noise_share": round(noise / len(labels), 4),
        "noise_by_platform": noise_df.to_dict("records"),
        "coherent": n_coh, "off_topic_geopolitical": n_off,
        "silhouette_cosine_1024d": round(sil, 4),
        "silhouette_space": "original 1024-d embedding, cosine -- NOT UMAP space",
        "stability": st,
        "params": {"min_cluster_size": mcs, "min_samples": MIN_SAMPLES,
                   "umap_components": UMAP_COMPONENTS, "umap_neighbors": UMAP_NEIGHBORS,
                   "n_parents": args.parents, "primary_seed": primary,
                   "primary_seed_rule": "highest mean pairwise ARI to the other seeds"},
    }

    if args.dry_run:
        log("DRY-RUN: nothing written. Would have written "
            f"{T['dim']}={len(topic_dim):,} {T['parent']}={len(parent_dim):,} "
            f"{T['post']}={len(post_topic):,} {T['noise']}={len(noise_df):,} "
            f"{T['xw']}={len(xw):,} rows to {out_db}")
    else:
        out_db.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(out_db), read_only=False)
        for name, payload in [(T["dim"], topic_dim), (T["parent"], parent_dim),
                              (T["post"], post_topic), (T["xw"], xw)]:
            if payload is None or not len(payload):
                log(f"  (skipping {name}: empty)")
                continue
            con.register("_p", payload)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT {_projection(payload)} FROM _p")
            con.unregister("_p")
            log(f"  published {name:<26} rows={len(payload):>7,}")
        # noise table: explicit DDL, then INSERT
        con.execute(NOISE_DDL.format(name=T["noise"]))
        con.register("_p", noise_df[["platform", "posts", "noise", "noise_pct"]])
        con.execute(f"INSERT INTO {T['noise']} SELECT platform, posts, noise, noise_pct FROM _p")
        con.unregister("_p")
        log(f"  published {T['noise']:<26} rows={len(noise_df):>7,}")
        con.close()

        report = REPORT.with_name(f"ontology_{args.out_version}_summary.json")
        report.parent.mkdir(parents=True, exist_ok=True)
        summary["timings"] = stages.report()
        summary["peak_rss_gb"] = round(peak_rss_gb(), 2)
        report.write_text(json.dumps(summary, indent=2, default=str))
        log(f"wrote {report}")

    stages.print()
    log(f"peak RSS: {peak_rss_gb():.1f} GB")
    log(f"done in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
