#!/usr/bin/env python3
"""Compute Improvised Cross-Platform Reach & Narrative Diffusion.

Implements Section 9 of the Virality & Reach Implementation Brief:
1. Cross-Platform Unified Story Reach (Multi-Platform Aggregation)
2. Network Diffusion Breadth Multiplier
3. Cross-Platform View Imputation for View-Less Platforms
4. Updates metrics.duckdb (cross_platform_stories, cross_platform_posts)
5. Updates UI/data/metrics.json and UI/data/metrics.json.gz
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
UI_DATA = ROOT / "UI" / "data"

ANALYTICS_DB = DATA / "analytics.duckdb"
METRICS_DB = DATA / "metrics.duckdb"
METRICS_JSON = UI_DATA / "metrics.json"
METRICS_GZ = UI_DATA / "metrics.json.gz"


def compute_cross_platform_reach(limit_stories: int = 150):
    print("=" * 72)
    print("Computing Improvised Cross-Platform Reach & Narrative Clusters")
    print(f"Reading from: {ANALYTICS_DB}")
    print("=" * 72)

    con = duckdb.connect(str(ANALYTICS_DB), read_only=True)

    query = f"""
    WITH cleaned AS (
        SELECT 
            post_uid,
            platform,
            author_name,
            posted_at,
            url,
            coalesce(likes, 0) AS likes,
            coalesce(comments, 0) AS comments,
            coalesce(shares, 0) AS shares,
            coalesce(views, 0) AS views,
            (coalesce(likes, 0) + coalesce(comments, 0) + coalesce(shares, 0)) AS engagement,
            trim(regexp_replace(lower(text), '\\s+', ' ', 'g')) AS clean_text,
            left(text, 220) AS raw_snippet
        FROM post_facts
        WHERE text IS NOT NULL AND length(trim(text)) >= 35
    ),
    clusters AS (
        SELECT 
            clean_text,
            any_value(raw_snippet) AS snippet,
            count(DISTINCT platform) AS n_platforms,
            count(*) AS n_posts,
            min(posted_at) AS origin_time,
            max(posted_at) AS latest_time,
            arg_min(platform, posted_at) AS origin_platform,
            list(DISTINCT platform) AS platforms,
            sum(engagement) AS total_engagement,
            sum(views) AS total_views,
            sum(CASE WHEN platform IN ('telegram', 'tiktok', 'youtube', 'twitter') AND views > 0 THEN views ELSE 0 END) AS observed_views,
            sum(CASE WHEN platform IN ('telegram', 'tiktok', 'youtube', 'twitter') AND views > 0 THEN engagement ELSE 0 END) AS paired_engagement,
            sum(CASE WHEN platform NOT IN ('telegram', 'tiktok', 'youtube') AND (views = 0 OR views IS NULL) THEN engagement ELSE 0 END) AS viewless_engagement
        FROM cleaned
        WHERE clean_text NOT SIMILAR TO '^[0-9\\s\\.\\,\\:\\-\\_\\#\\@]+$'
        GROUP BY clean_text
        HAVING count(DISTINCT platform) >= 2
    ),
    calculated AS (
        SELECT 
            *,
            round(epoch(latest_time - origin_time) / 3600.0, 1) AS spread_duration_hours,
            md5(clean_text) AS story_id,
            CASE 
                WHEN paired_engagement > 0 AND observed_views > 0 THEN 
                    LEAST(GREATEST(round(observed_views * 1.0 / paired_engagement, 2), 5.0), 80.0)
                ELSE 20.0 
            END AS story_ratio
        FROM clusters
    ),
    improvised AS (
        SELECT 
            story_id,
            clean_text,
            snippet,
            n_platforms,
            n_posts,
            origin_platform,
            origin_time::varchar AS origin_time,
            spread_duration_hours,
            platforms,
            total_engagement,
            total_views AS raw_views,
            observed_views,
            story_ratio,
            round(viewless_engagement * story_ratio, 0) AS imputed_views,
            round(observed_views + (viewless_engagement * story_ratio), 0) AS unified_reach,
            round(1.0 + 0.5 * (n_platforms - 1), 2) AS breadth_multiplier,
            round((observed_views + (viewless_engagement * story_ratio)) * (1.0 + 0.5 * (n_platforms - 1)), 0) AS effective_reach
        FROM calculated
        ORDER BY n_platforms DESC, effective_reach DESC, n_posts DESC
        LIMIT {limit_stories}
    )
    SELECT * FROM improvised
    """

    stories_df = con.execute(query).fetchdf()
    print(f"-> Extracted {len(stories_df)} multi-platform stories with improvised reach.")

    # Extract matching posts
    top_story_hashes = [r["clean_text"] for r in stories_df[["clean_text"]].to_dict(orient="records")]
    con.register("top_stories", stories_df[["story_id", "clean_text"]])

    posts_query = """
    WITH cleaned AS (
        SELECT 
            post_uid,
            platform,
            posted_at,
            trim(regexp_replace(lower(text), '\\s+', ' ', 'g')) AS clean_text
        FROM post_facts
        WHERE text IS NOT NULL AND length(trim(text)) >= 35
    )
    SELECT 
        s.story_id,
        c.post_uid,
        c.platform,
        c.posted_at::varchar AS posted_at
    FROM cleaned c
    JOIN top_stories s ON s.clean_text = c.clean_text
    ORDER BY s.story_id, c.posted_at ASC
    """
    posts_df = con.execute(posts_query).fetchdf()
    print(f"-> Extracted {len(posts_df)} individual posts across narrative clusters.")
    con.close()

    # Drop clean_text before saving to public tables
    stories_df = stories_df.drop(columns=["clean_text"])

    # Convert platforms array to list of strings
    stories_df["platforms"] = stories_df["platforms"].apply(lambda p: list(p) if hasattr(p, "__iter__") else [str(p)])

    return stories_df, posts_df


def save_to_duckdb(stories_df: pd.DataFrame, posts_df: pd.DataFrame):
    print(f"Writing to: {METRICS_DB}")
    mcon = duckdb.connect(str(METRICS_DB), read_only=False)
    mcon.execute("DROP TABLE IF EXISTS cross_platform_stories")
    mcon.execute("CREATE TABLE cross_platform_stories AS SELECT * FROM stories_df")
    mcon.execute("DROP TABLE IF EXISTS cross_platform_posts")
    mcon.execute("CREATE TABLE cross_platform_posts AS SELECT * FROM posts_df")
    mcon.execute("CREATE INDEX IF NOT EXISTS idx_cp_posts_story ON cross_platform_posts(story_id)")
    mcon.execute("CREATE INDEX IF NOT EXISTS idx_cp_posts_uid ON cross_platform_posts(post_uid)")
    n_stories = mcon.execute("SELECT count(*) FROM cross_platform_stories").fetchone()[0]
    n_posts = mcon.execute("SELECT count(*) FROM cross_platform_posts").fetchone()[0]
    mcon.close()
    print(f"-> Successfully saved {n_stories} stories and {n_posts} posts into metrics.duckdb.")


def update_metrics_json(stories_df: pd.DataFrame):
    if not METRICS_JSON.exists():
        print(f"Warning: {METRICS_JSON} not found. Skipping JSON update.")
        return

    print(f"Updating: {METRICS_JSON}")
    with open(METRICS_JSON, "r", encoding="utf-8") as f:
        payload = json.load(f)

    # Convert dataframe to columnar format matching metrics.json
    cols = list(stories_df.columns)
    rows = []
    for tup in stories_df.itertuples(index=False, name=None):
        row_vals = []
        for v in tup:
            if hasattr(v, "tolist"):
                row_vals.append(v.tolist())
            elif isinstance(v, (list, tuple)):
                row_vals.append(list(v))
            elif isinstance(v, float) and (pd.isna(v) or not v == v):
                row_vals.append(None)
            elif isinstance(v, int):
                row_vals.append(int(v))
            elif isinstance(v, float):
                row_vals.append(round(v, 2))
            else:
                row_vals.append(v)
        rows.append(row_vals)

    payload["tables"]["cross_platform_stories"] = {
        "columns": cols,
        "rows": rows,
        "n": len(rows),
        "table": "cross_platform_stories"
    }

    raw_json = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    with open(METRICS_JSON, "w", encoding="utf-8") as f:
        f.write(raw_json)
    print(f"-> Saved updated metrics.json ({len(raw_json) / (1024*1024):.2f} MB)")

    # Build gzip
    print(f"Compressing to: {METRICS_GZ}")
    gz_bytes = gzip.compress(raw_json.encode("utf-8"), compresslevel=6)
    with open(METRICS_GZ, "wb") as f:
        f.write(gz_bytes)
    print(f"-> Saved metrics.json.gz ({len(gz_bytes) / (1024*1024):.2f} MB)")


def main():
    stories_df, posts_df = compute_cross_platform_reach()
    save_to_duckdb(stories_df, posts_df)
    update_metrics_json(stories_df)
    print("=" * 72)
    print("Improvised Reach Computation & Publication Complete!")
    print("=" * 72)


if __name__ == "__main__":
    main()

