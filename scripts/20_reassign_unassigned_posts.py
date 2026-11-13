#!/usr/bin/env python3
"""
Reassign unassigned posts to real topics using nearest-centroid.

WHY THIS SCRIPT EXISTS
----------------------
In post_topic, ~39,778 posts are labelled 'unassigned':
  - 29,961 posts (noise_low_conf): Had embeddings but cosine similarity to nearest
    cluster centroid was below the original 0.55 threshold, so they were dumped.
  - 9,817 posts (no_embedding): No embedding existed at clustering time.

This script fixes the first group (noise_low_conf) immediately by computing
cluster centroids from the existing confirmed assignments and forcing every
noise post to its nearest real topic (using a lower threshold of 0.30 instead
of 0.55 — based on the measured avg confidence of 0.52 for these posts).

The second group (no_embedding) is handled only if new embeddings have been
extracted and loaded into post_embeddings.

Writes results back to post_topic in analytics.duckdb (replaces unassigned rows).
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
# Use the Social-Media-Metrics analytics.duckdb which has intact post_embeddings table
DB = Path("/data/kavya/MetricsCritique-main/Social-Media-Metrics/MetricsCritique/data/analytics.duckdb")

# Lower confidence threshold: original was 0.55
# noise_low_conf posts have avg similarity of 0.52 to their best centroid,
# so lowering to 0.30 captures almost all of them into real topics.
CONF_LOW_NEW = 0.30


def log(msg):
    print(f"[20] {msg}", flush=True)


def main() -> int:
    if not DB.exists():
        print(f"ERROR: DB not found: {DB}")
        return 1

    log(f"Opening {DB} ...")
    con = duckdb.connect(str(DB), read_only=False)

    # --- Step 1: Load embeddings for confirmed (non-unassigned) posts to build centroids ---
    log("Loading confirmed post assignments + embeddings to build centroids ...")
    confirmed = con.execute("""
        SELECT pt.post_uid, pt.topic_id, pt.cluster_id, pe.embedding
        FROM post_topic pt
        JOIN post_embeddings pe ON pt.post_uid = pe.post_uid
        WHERE pt.topic_id != 'unassigned'
          AND pt.assign_method IN ('umap_hdbscan', 'nearest_centroid')
    """).fetchdf()
    log(f"  confirmed posts with embeddings: {len(confirmed):,}")

    # Build per-topic centroids (unit-normalised mean embedding)
    log("Building cluster centroids ...")
    topic_to_cluster = dict(zip(confirmed["topic_id"], confirmed["cluster_id"]))
    centroids = {}
    for tid, grp in confirmed.groupby("topic_id"):
        embs = np.vstack(grp["embedding"].apply(np.array).values).astype(np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        embs = embs / norms
        c = embs.mean(axis=0)
        c = c / (np.linalg.norm(c) + 1e-9)
        centroids[tid] = c
    log(f"  built centroids for {len(centroids)} real topics")

    # --- Step 2: Load unassigned posts that have embeddings (noise_low_conf) ---
    log("Loading noise_low_conf unassigned posts (have embeddings) ...")
    noise = con.execute("""
        SELECT pt.post_uid, pe.embedding
        FROM post_topic pt
        JOIN post_embeddings pe ON pt.post_uid = pe.post_uid
        WHERE pt.topic_id = 'unassigned'
          AND pt.assign_method = 'noise_low_conf'
    """).fetchdf()
    log(f"  noise_low_conf posts to reassign: {len(noise):,}")

    # Also try no_embedding posts that may have gotten embeddings now
    log("Loading no_embedding posts that may now have embeddings ...")
    newly_embedded = con.execute("""
        SELECT pt.post_uid, pe.embedding
        FROM post_topic pt
        JOIN post_embeddings pe ON pt.post_uid = pe.post_uid
        WHERE pt.topic_id = 'unassigned'
          AND pt.assign_method = 'no_embedding'
    """).fetchdf()
    log(f"  previously no_embedding posts now with embeddings: {len(newly_embedded):,}")

    # Combine both groups
    all_to_reassign = pd.concat([noise, newly_embedded], ignore_index=True)
    log(f"  total posts to reassign: {len(all_to_reassign):,}")

    if all_to_reassign.empty:
        log("Nothing to reassign. Exiting.")
        con.close()
        return 0

    # --- Step 3: Assign each post to nearest centroid ---
    log("Reassigning posts to nearest centroid ...")
    centroid_matrix = np.vstack(list(centroids.values())).astype(np.float32)
    topic_ids_ordered = list(centroids.keys())

    new_rows = []
    assigned_count = 0
    still_unassigned = 0

    for _, row in all_to_reassign.iterrows():
        vec = np.array(row["embedding"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm == 0:
            still_unassigned += 1
            new_rows.append({
                "post_uid": row["post_uid"],
                "topic_id": "unassigned",
                "cluster_id": -1,
                "confidence": 0.0,
                "assign_method": "noise_low_conf"
            })
            continue

        vec = vec / norm
        sims = centroid_matrix @ vec  # cosine similarities
        best_idx = int(np.argmax(sims))
        best_tid = topic_ids_ordered[best_idx]
        best_conf = float(sims[best_idx])

        if best_conf >= CONF_LOW_NEW:
            assigned_count += 1
            new_rows.append({
                "post_uid": row["post_uid"],
                "topic_id": best_tid,
                "cluster_id": int(topic_to_cluster.get(best_tid, -1)),
                "confidence": round(best_conf, 6),
                "assign_method": "nearest_centroid_v2"
            })
        else:
            still_unassigned += 1
            new_rows.append({
                "post_uid": row["post_uid"],
                "topic_id": "unassigned",
                "cluster_id": -1,
                "confidence": round(best_conf, 6),
                "assign_method": "noise_low_conf"
            })

    log(f"  newly assigned to real topics: {assigned_count:,}")
    log(f"  still unassigned (below {CONF_LOW_NEW}): {still_unassigned:,}")

    new_df = pd.DataFrame(new_rows)

    # --- Step 4: Update post_topic in DuckDB ---
    log("Updating post_topic table ...")
    con.register("_new_assignments", new_df)

    # Delete old unassigned rows that we are replacing
    uids = new_df["post_uid"].tolist()
    con.execute("""
        DELETE FROM post_topic
        WHERE post_uid IN (SELECT post_uid FROM _new_assignments)
    """)
    log(f"  deleted {len(uids):,} old unassigned rows")

    # Insert updated assignments
    con.execute("""
        INSERT INTO post_topic (post_uid, topic_id, cluster_id, confidence, assign_method)
        SELECT post_uid, topic_id, cluster_id, confidence, assign_method
        FROM _new_assignments
    """)
    log(f"  inserted {len(new_df):,} updated rows")

    con.unregister("_new_assignments")

    # --- Step 5: Verify final state ---
    final = con.execute("""
        SELECT topic_id, COUNT(*) as cnt
        FROM post_topic
        GROUP BY topic_id
        ORDER BY cnt DESC
    """).fetchdf()
    log("Final topic distribution:")
    print(final.to_string(index=False))

    still_unassigned_total = int(final[final["topic_id"] == "unassigned"]["cnt"].sum()) if "unassigned" in final["topic_id"].values else 0
    log(f"\nResult: 'unassigned' posts remaining: {still_unassigned_total:,}")

    con.close()
    log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
