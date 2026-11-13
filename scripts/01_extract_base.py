#!/usr/bin/env python3
"""
Layer 0 — Read-only extract from shaktidb_social → local parquet + DuckDB.

- Single REPEATABLE READ snapshot (internally consistent)
- Production Postgres: SELECT only; always ROLLBACK
- Dedup into post_facts; seed/append engagement_snapshots
- Never writes back to Postgres
"""
from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Allow `python scripts/01_extract_base.py` from EDA/ or scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DUCKDB_PATH, EMBEDDING_MODEL, PARQUET_DIR
from lib.duck import (
    append_parquet,
    connect,
    ensure_dirs,
    register_parquet_views,
    replace_table_from_df,
    write_parquet,
)
from lib.pg import q, q1, read_only_snapshot


def _uid(*parts: str) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def _md5_text(text: str | None) -> str:
    if not text:
        return ""
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _as_ts(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return pd.NaT
    return pd.to_datetime(v, utc=True, errors="coerce")


def _lag_days(posted, scraped):
    if pd.isna(posted) or pd.isna(scraped):
        return np.nan
    return (scraped - posted).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Per-platform extract SQL → common columns
# ---------------------------------------------------------------------------
# Common columns:
# platform, native_id, url, author_id, author_name, is_verified, text, lang,
# posted_at, scraped_at, keyword, hashtags, likes, comments, shares, views,
# has_engagement

PLATFORM_SQL = {
    "facebook": """
        SELECT
            'facebook'::text AS platform,
            COALESCE(post_id::text, url) AS native_id,
            url,
            author_id::text AS author_id,
            author_name,
            is_verified,
            post_text AS text,
            NULL::text AS lang,
            posted_time AS posted_at,
            scraped_at,
            keywords AS keyword,
            NULL::text AS hashtags,
            reactions_count AS likes,
            comments_count AS comments,
            shares_count AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM facebook_posts
    """,
    "instagram": """
        SELECT
            'instagram'::text AS platform,
            COALESCE(id::text, post_url) AS native_id,
            post_url AS url,
            user_name AS author_id,
            user_name AS author_name,
            is_verified,
            caption AS text,
            NULL::text AS lang,
            posted_at,
            scraped_at,
            keyword,
            hashtags::text AS hashtags,
            likes_count AS likes,
            comment_count AS comments,
            shares_count AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM instagram_posts
    """,
    "linkedin": """
        SELECT
            'linkedin'::text AS platform,
            post_url AS native_id,
            post_url AS url,
            author_page AS author_id,
            author_page AS author_name,
            NULL::boolean AS is_verified,
            main_text AS text,
            NULL::text AS lang,
            posted_at,
            scraped_at,
            keyword,
            hashtags::text AS hashtags,
            reaction_count AS likes,
            comment_count AS comments,
            NULL::double precision AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM linkedin_posts
    """,
    "pinterest": """
        SELECT
            'pinterest'::text AS platform,
            COALESCE(pin_id::text, pin_url, id::text) AS native_id,
            pin_url AS url,
            creator_username AS author_id,
            creator_name AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(description, title, comment_text) AS text,
            NULL::text AS lang,
            posted_at,
            scraped_at,
            keyword,
            hashtags::text AS hashtags,
            NULL::double precision AS likes,
            comment_count AS comments,
            NULL::double precision AS shares,
            NULL::double precision AS views,
            (comment_count IS NOT NULL) AS has_engagement,
            embedding
        FROM pinterest_posts
    """,
    "reddit": """
        SELECT
            'reddit'::text AS platform,
            id::text AS native_id,
            url,
            author AS author_id,
            author AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(body, title) AS text,
            NULL::text AS lang,
            posted_at,
            scraped_at,
            keyword,
            NULL::text AS hashtags,
            NULL::double precision AS likes,
            NULL::double precision AS comments,
            NULL::double precision AS shares,
            NULL::double precision AS views,
            FALSE AS has_engagement,
            embedding
        FROM reddit_posts
    """,
    "snapchat": """
        SELECT
            'snapchat'::text AS platform,
            id::text AS native_id,
            spotlight_url AS url,
            creator AS author_id,
            creator AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(caption, title) AS text,
            language AS lang,
            posted_at,
            scraped_at,
            keyword,
            hashtags::text AS hashtags,
            likes AS likes,
            COALESCE(comments, comment_count) AS comments,
            shares AS shares,
            view_count AS views,
            TRUE AS has_engagement,
            embedding
        FROM snapchat_posts
    """,
    "telegram": """
        SELECT
            'telegram'::text AS platform,
            COALESCE(post_url, channel_id::text || ':' || message_id::text) AS native_id,
            post_url AS url,
            channel_id::text AS author_id,
            channel_name AS author_name,
            NULL::boolean AS is_verified,
            text,
            language AS lang,
            posted_at,
            scraped_at,
            NULL::text AS keyword,
            NULL::text AS hashtags,
            NULL::double precision AS likes,
            NULL::double precision AS comments,
            forwards AS shares,
            views AS views,
            TRUE AS has_engagement,
            embedding
        FROM telegram_posts
    """,
    "youtube_video": """
        SELECT
            'youtube_video'::text AS platform,
            video_id AS native_id,
            video_url AS url,
            channel_id AS author_id,
            channel_name AS author_name,
            is_verified,
            COALESCE(video_title, '') || E'\\n' || COALESCE(video_description, '')
                || E'\\n' || COALESCE(LEFT(transcript_text, 4000), '') AS text,
            transcript_language AS lang,
            video_published_at AS posted_at,
            scraped_at,
            keyword,
            NULL::text AS hashtags,
            video_like_count AS likes,
            video_comment_count AS comments,
            NULL::double precision AS shares,
            video_view_count AS views,
            TRUE AS has_engagement,
            NULL::vector AS embedding
        FROM youtube_videos
    """,
    "twitter": """
        SELECT
            'twitter'::text AS platform,
            COALESCE(tweet_id::text, id::text) AS native_id,
            post_url AS url,
            COALESCE(author_id::text, author_username) AS author_id,
            COALESCE(author_name, author_username) AS author_name,
            is_verified,
            COALESCE(translated_text, text) AS text,
            COALESCE(detected_language, language) AS lang,
            posted_at,
            scraped_at,
            keyword,
            hashtags::text AS hashtags,
            like_count AS likes,
            reply_count AS comments,
            retweet_count AS shares,
            impression_count::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM twitter_posts
    """,
    "whatsapp": """
        SELECT
            'whatsapp'::text AS platform,
            COALESCE(channel_id::text || ':' || message_id::text, id::text) AS native_id,
            COALESCE(media_link, external_link) AS url,
            channel_id::text AS author_id,
            channel_id::text AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(translated_text, text, translated_caption, caption, '') AS text,
            COALESCE(detected_language, language) AS lang,
            posted_at,
            scraped_at,
            NULL::text AS keyword,
            NULL::text AS hashtags,
            NULL::double precision AS likes,
            NULL::double precision AS comments,
            NULL::double precision AS shares,
            NULL::double precision AS views,
            FALSE AS has_engagement,
            embedding
        FROM whatsapp_posts
    """,
    "tiktok": """
        SELECT
            'tiktok'::text AS platform,
            post_id::text AS native_id,
            COALESCE(share_url, video_url) AS url,
            COALESCE(author_uid::text, author_username) AS author_id,
            COALESCE(author_nickname, author_username) AS author_name,
            (author_verification_type IS NOT NULL AND author_verification_type != '0') AS is_verified,
            COALESCE(translated_desc, "desc") AS text,
            COALESCE(detected_language, language, desc_language) AS lang,
            to_timestamp(create_time) AS posted_at,   -- create_time is a bigint of Unix SECONDS; unconverted it became 1970-01-01
            collected_at AS scraped_at,
            keyword,
            NULL::text AS hashtags,
            digg_count AS likes,
            comment_count AS comments,
            share_count AS shares,
            play_count::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM tiktok_posts
    """,
    "baidu_tieba": """
        SELECT
            'baidu_tieba'::text AS platform,
            COALESCE(pid::text, tid::text, id::text) AS native_id,
            post_url AS url,
            COALESCE(author_id::text, author_name) AS author_id,
            author_name AS author_name,
            is_verified,
            COALESCE(translated_content, content, translated_title, title) AS text,
            COALESCE(detected_language, language) AS lang,
            posted_time AS posted_at,
            collected_time AS scraped_at,
            keyword_used AS keyword,
            NULL::text AS hashtags,
            like_count AS likes,
            reply_count AS comments,
            NULL::double precision AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM baidu_tieba_posts
    """,
    "baidu_video": """
        SELECT
            'baidu_video'::text AS platform,
            id::text AS native_id,
            COALESCE(video_url, redirect_url) AS url,
            author AS author_id,
            author AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(translated_title, title) AS text,
            COALESCE(detected_language, language) AS lang,
            posted_time AS posted_at,
            collected_time AS scraped_at,
            keyword_used AS keyword,
            NULL::text AS hashtags,
            likes AS likes,
            comment_count AS comments,
            share_count AS shares,
            view_count::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM baidu_video_posts
    """,
    "mastodon": """
        SELECT
            'mastodon'::text AS platform,
            id::text AS native_id,
            url,
            COALESCE(account_id::text, account_acct) AS author_id,
            COALESCE(account_display_name, account_acct) AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(translated_content_text, content_text, content_html) AS text,
            COALESCE(detected_language, language) AS lang,
            created_at AS posted_at,
            collected_at AS scraped_at,
            matched_hashtag AS keyword,
            tags::text AS hashtags,
            favourites_count AS likes,
            replies_count AS comments,
            reblogs_count AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM mastodon_posts
    """,
    "truthsocial": """
        SELECT
            'truthsocial'::text AS platform,
            post_id::text AS native_id,
            url,
            COALESCE(author_id::text, author_handle) AS author_id,
            COALESCE(author_display_name, author_handle) AS author_name,
            NULL::boolean AS is_verified,
            COALESCE(translated_text_plain, text_plain, text_html) AS text,
            COALESCE(detected_language, language) AS lang,
            created_at AS posted_at,
            collected_at AS scraped_at,
            topic AS keyword,
            NULL::text AS hashtags,
            favourites_count AS likes,
            replies_count AS comments,
            reblogs_count AS shares,
            NULL::double precision AS views,
            TRUE AS has_engagement,
            embedding
        FROM truthsocial_posts
    """,
    "facebook_profile": """
        SELECT
            'facebook_profile'::text AS platform,
            COALESCE(post_id::text, url, id::text) AS native_id,
            url,
            COALESCE(author_id::text, author_name) AS author_id,
            author_name AS author_name,
            is_verified,
            COALESCE(translated_message, message, translated_message_rich, message_rich) AS text,
            COALESCE(detected_language, language) AS lang,
            COALESCE(posted_time, to_timestamp(timestamp)) AS posted_at,
            collected_time AS scraped_at,
            NULL::text AS keyword,
            NULL::text AS hashtags,
            reactions_count AS likes,
            comments_count AS comments,
            reshare_count AS shares,
            video_view_count::double precision AS views,
            TRUE AS has_engagement,
            NULL::vector AS embedding
        FROM facebook_profile_posts
    """,
    # ---- DISABLED for now (local storage): discord / chirpwire / weibo. The SQL below was validated against
    # ---- production (read-only). To enable: un-comment this block, the matching EFFORT_SQLS block, the three
    # ---- table names in the row-count list and the emb_specs lines in main(), and add discord + chirpwire to
    # ---- NO_ENGAGEMENT_PLATFORMS in lib/config.py (they have no engagement counts today).
    # "discord": """
    #     SELECT
    #         'discord'::text AS platform,
    #         message_id::text AS native_id,
    #         url,
    #         author AS author_id,
    #         author AS author_name,
    #         NULL::boolean AS is_verified,
    #         content AS text,
    #         NULL::text AS lang,
    #         "timestamp" AS posted_at,
    #         collected_time AS scraped_at,
    #         keyword_used AS keyword,
    #         NULL::text AS hashtags,
    #         NULL::double precision AS likes,
    #         NULL::double precision AS comments,
    #         NULL::double precision AS shares,
    #         NULL::double precision AS views,
    #         FALSE AS has_engagement,
    #         NULL::vector AS embedding
    #     FROM discord_posts
    # """,
    # "chirpwire": """
    #     SELECT
    #         'chirpwire'::text AS platform,
    #         post_id::text AS native_id,
    #         thread_url AS url,
    #         author_handle AS author_id,
    #         COALESCE(author_name, author_handle) AS author_name,
    #         NULL::boolean AS is_verified,
    #         COALESCE(translated_post_text, post_text) AS text,
    #         COALESCE(detected_language, language) AS lang,
    #         posted_at,
    #         fetched_at AS scraped_at,
    #         hashtag_queried AS keyword,
    #         hashtags::text AS hashtags,
    #         likes_count::double precision AS likes,
    #         comments_count::double precision AS comments,
    #         reposts_count::double precision AS shares,
    #         NULL::double precision AS views,
    #         (likes_count IS NOT NULL OR comments_count IS NOT NULL OR reposts_count IS NOT NULL) AS has_engagement,
    #         embedding
    #     FROM chirpwire_posts
    # """,
    # "weibo": """
    #     SELECT
    #         'weibo'::text AS platform,
    #         weibo_id::text AS native_id,
    #         post_url AS url,
    #         author_id::text AS author_id,
    #         author_name,
    #         NULL::boolean AS is_verified,
    #         COALESCE(translated_text, text) AS text,
    #         COALESCE(detected_language, language) AS lang,
    #         posted_at,
    #         scraped_at,
    #         keyword,
    #         topics::text AS hashtags,
    #         attitudes_count::double precision AS likes,
    #         comments_count::double precision AS comments,
    #         reposts_count::double precision AS shares,
    #         NULL::double precision AS views,
    #         TRUE AS has_engagement,
    #         embedding
    #     FROM weibo_posts
    # """,
}

COMMENT_SQL = """
    SELECT
        comment_id,
        video_id,
        parent_comment_id,
        is_reply,
        author_display_name,
        author_channel_id,
        comment_text AS text,
        like_count AS likes,
        published_at AS posted_at,
        scraped_at,
        embedding IS NOT NULL AS has_embedding
    FROM youtube_comments
"""

EFFORT_SQLS = {
    "telegram": """
        SELECT
            'telegram'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT channel_id) AS active_channels,
            NULL::bigint AS active_keywords
        FROM telegram_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "youtube_video": """
        SELECT
            'youtube_video'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT channel_id) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM youtube_videos
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "youtube_comment": """
        SELECT
            'youtube_comment'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_channel_id) AS active_channels,
            NULL::bigint AS active_keywords
        FROM youtube_comments
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "reddit": """
        SELECT
            'reddit'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM reddit_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "linkedin": """
        SELECT
            'linkedin'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_page) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM linkedin_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "instagram": """
        SELECT
            'instagram'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT user_name) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM instagram_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "facebook": """
        SELECT
            'facebook'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_id) AS active_channels,
            COUNT(DISTINCT keywords) AS active_keywords
        FROM facebook_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "snapchat": """
        SELECT
            'snapchat'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT creator) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM snapchat_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "pinterest": """
        SELECT
            'pinterest'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT creator_username) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM pinterest_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "twitter": """
        SELECT
            'twitter'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_id) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM twitter_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "whatsapp": """
        SELECT
            'whatsapp'::text AS platform,
            date_trunc('day', scraped_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT channel_id) AS active_channels,
            NULL::bigint AS active_keywords
        FROM whatsapp_posts
        WHERE scraped_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "tiktok": """
        SELECT
            'tiktok'::text AS platform,
            date_trunc('day', collected_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_uid) AS active_channels,
            COUNT(DISTINCT keyword) AS active_keywords
        FROM tiktok_posts
        WHERE collected_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "baidu_tieba": """
        SELECT
            'baidu_tieba'::text AS platform,
            date_trunc('day', collected_time)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT forum) AS active_channels,
            COUNT(DISTINCT keyword_used) AS active_keywords
        FROM baidu_tieba_posts
        WHERE collected_time IS NOT NULL
        GROUP BY 1, 2
    """,
    "baidu_video": """
        SELECT
            'baidu_video'::text AS platform,
            date_trunc('day', collected_time)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author) AS active_channels,
            COUNT(DISTINCT keyword_used) AS active_keywords
        FROM baidu_video_posts
        WHERE collected_time IS NOT NULL
        GROUP BY 1, 2
    """,
    "mastodon": """
        SELECT
            'mastodon'::text AS platform,
            date_trunc('day', collected_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT account_id) AS active_channels,
            COUNT(DISTINCT matched_hashtag) AS active_keywords
        FROM mastodon_posts
        WHERE collected_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "truthsocial": """
        SELECT
            'truthsocial'::text AS platform,
            date_trunc('day', collected_at)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_id) AS active_channels,
            COUNT(DISTINCT topic) AS active_keywords
        FROM truthsocial_posts
        WHERE collected_at IS NOT NULL
        GROUP BY 1, 2
    """,
    "facebook_profile": """
        SELECT
            'facebook_profile'::text AS platform,
            date_trunc('day', collected_time)::date AS day,
            COUNT(*) AS rows_ingested,
            COUNT(DISTINCT author_id) AS active_channels,
            NULL::bigint AS active_keywords
        FROM facebook_profile_posts
        WHERE collected_time IS NOT NULL
        GROUP BY 1, 2
    """,
    # "discord": """
    #     SELECT
    #         'discord'::text AS platform,
    #         date_trunc('day', collected_time)::date AS day,
    #         COUNT(*) AS rows_ingested,
    #         COUNT(DISTINCT channel_id) AS active_channels,
    #         COUNT(DISTINCT keyword_used) AS active_keywords
    #     FROM discord_posts
    #     WHERE collected_time IS NOT NULL
    #     GROUP BY 1, 2
    # """,
    # "chirpwire": """
    #     SELECT
    #         'chirpwire'::text AS platform,
    #         date_trunc('day', fetched_at)::date AS day,
    #         COUNT(*) AS rows_ingested,
    #         COUNT(DISTINCT author_handle) AS active_channels,
    #         COUNT(DISTINCT hashtag_queried) AS active_keywords
    #     FROM chirpwire_posts
    #     WHERE fetched_at IS NOT NULL
    #     GROUP BY 1, 2
    # """,
    # "weibo": """
    #     SELECT
    #         'weibo'::text AS platform,
    #         date_trunc('day', scraped_at)::date AS day,
    #         COUNT(*) AS rows_ingested,
    #         COUNT(DISTINCT author_id) AS active_channels,
    #         COUNT(DISTINCT keyword) AS active_keywords
    #     FROM weibo_posts
    #     WHERE scraped_at IS NOT NULL
    #     GROUP BY 1, 2
    # """,
}


def fetch_platform(cur, platform: str, sql: str) -> pd.DataFrame:
    print(f"  extracting {platform} ...", flush=True)
    cols, rows = q(cur, sql)
    if not rows:
        print(f"    {platform}: 0 rows")
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=cols)
    print(f"    {platform}: {len(df):,} raw rows")
    return df


def normalize_and_dedup(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (post_facts without embeddings, post_embeddings)."""
    if df.empty:
        empty_facts = pd.DataFrame(
            columns=[
                "post_uid", "platform", "native_id", "url", "author_uid", "author_name",
                "is_verified", "text", "lang", "posted_at", "scraped_at", "scrape_lag_days",
                "keyword", "hashtags", "likes", "comments", "shares", "views", "has_engagement",
            ]
        )
        empty_emb = pd.DataFrame(
            columns=["post_uid", "platform", "embedding", "embedding_model", "embedding_dim"]
        )
        return empty_facts, empty_emb

    df = df.copy()
    df["posted_at"] = df["posted_at"].map(_as_ts)
    df["scraped_at"] = df["scraped_at"].map(_as_ts)
    df["scrape_lag_days"] = [
        _lag_days(p, s) for p, s in zip(df["posted_at"], df["scraped_at"])
    ]

    # Prefer last scraped when deduping
    df["_scraped_sort"] = df["scraped_at"].fillna(pd.Timestamp.min.tz_localize("UTC"))
    df = df.sort_values("_scraped_sort")

    # Dedup key: (platform, native_id) when native_id present, else (platform, md5(text), author)
    def make_uid(row):
        plat = row["platform"]
        nid = row["native_id"]
        if nid is not None and str(nid).strip() != "":
            return _uid(plat, str(nid))
        return _uid(plat, _md5_text(row.get("text")), str(row.get("author_id") or ""))

    df["post_uid"] = df.apply(make_uid, axis=1)
    before = len(df)
    # Keep last observation per post_uid
    df = df.drop_duplicates(subset=["post_uid"], keep="last")
    after = len(df)
    print(f"    dedup: {before:,} → {after:,} ({before - after:,} dropped)")

    emb_rows = []
    if "embedding" in df.columns:
        for _, row in df.iterrows():
            emb = row.get("embedding")
            if emb is None:
                continue
            # pgvector may come as list/str/memoryview
            if isinstance(emb, memoryview):
                continue
            if isinstance(emb, str):
                # format like "[0.1,0.2,...]"
                try:
                    emb = [float(x) for x in emb.strip("[]").split(",") if x.strip()]
                except ValueError:
                    continue
            elif hasattr(emb, "tolist"):
                emb = emb.tolist()
            if not emb:
                continue
            emb_rows.append(
                {
                    "post_uid": row["post_uid"],
                    "platform": row["platform"],
                    "embedding": list(emb),
                    "embedding_model": row.get("embedding_model") or EMBEDDING_MODEL,
                    "embedding_dim": len(emb),
                }
            )

    facts = pd.DataFrame(
        {
            "post_uid": df["post_uid"],
            "platform": df["platform"],
            "native_id": df["native_id"].astype(str),
            "url": df["url"],
            "author_uid": df["author_id"].map(lambda x: None if x is None else str(x)),
            "author_name": df["author_name"],
            "is_verified": df["is_verified"],
            "text": df["text"],
            "lang": df["lang"],
            "posted_at": df["posted_at"],
            "scraped_at": df["scraped_at"],
            "scrape_lag_days": df["scrape_lag_days"],
            "keyword": df["keyword"],
            "hashtags": df["hashtags"],
            "likes": pd.to_numeric(df["likes"], errors="coerce"),
            "comments": pd.to_numeric(df["comments"], errors="coerce"),
            "shares": pd.to_numeric(df["shares"], errors="coerce"),
            "views": pd.to_numeric(df["views"], errors="coerce"),
            "has_engagement": df["has_engagement"].astype(bool),
        }
    )
    emb_df = pd.DataFrame(emb_rows) if emb_rows else pd.DataFrame(
        columns=["post_uid", "platform", "embedding", "embedding_model", "embedding_dim"]
    )
    return facts, emb_df


def fetch_embeddings_separately(cur, platform: str, id_col: str, table: str, id_expr: str) -> dict:
    """Fallback: pull embeddings as text via CAST for tables that use vector type.

    NOTE: production platform tables no longer carry an `embedding_model` column
    (schema drift observed 2026-07-29 — only the `embedding` vector column remains
    on facebook/instagram/linkedin/pinterest/reddit/snapchat/telegram). We tag every
    fetched vector with the pipeline's own EMBEDDING_MODEL constant instead of trying
    to read a per-row model name that isn't there; this must stay in sync with
    whatever model actually produced the vectors upstream.
    """
    sql = f"""
        SELECT {id_expr} AS native_id, embedding::text AS emb_text
        FROM {table}
        WHERE embedding IS NOT NULL
    """
    try:
        cols, rows = q(cur, sql)
    except Exception as e:
        print(f"    embedding fetch for {platform} failed: {e}")
        # Don't let a failed SELECT leave the shared transaction aborted for the
        # *next* platform's query -- roll back and resume a fresh REPEATABLE READ
        # segment here, same recovery pattern used elsewhere in this script.
        try:
            cur.connection.rollback()
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
        except Exception:
            pass
        return {}
    out = {}
    for nid, emb_text in rows:
        if not emb_text:
            continue
        try:
            vec = [float(x) for x in emb_text.strip("[]").split(",") if x.strip()]
        except ValueError:
            continue
        out[str(nid)] = vec
    print(f"    embeddings for {platform}: {len(out):,}")
    return out


def main() -> int:
    ensure_dirs()
    extract_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    print(f"=== Layer 0 extract  extract_id={extract_id} ===")

    all_facts = []
    all_emb = []
    raw_counts = {}
    comment_df = pd.DataFrame()
    effort_frames = []

    with read_only_snapshot() as (conn, cur, snapshot_ts):
        print(f"snapshot_ts={snapshot_ts.isoformat()}")

        # Row counts for meta
        for t in [
            "facebook_posts", "facebook_profile_posts", "instagram_posts",
            "linkedin_posts", "pinterest_posts", "reddit_posts", "snapchat_posts",
            "telegram_posts", "whatsapp_posts", "twitter_posts", "tiktok_posts",
            "baidu_tieba_posts", "baidu_video_posts", "mastodon_posts",
            "truthsocial_posts",  # "discord_posts", "chirpwire_posts", "weibo_posts",  (disabled, see PLATFORM_SQL)
            "youtube_comments", "youtube_videos",
        ]:
            try:
                n = q1(cur, f'SELECT COUNT(*) FROM "{t}";')[0]
            except Exception:
                conn.rollback()
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
                n = -1
            raw_counts[t] = n
            print(f"  count {t}: {n}")

        # Posts per platform — fetch without embedding first if vector causes issues
        for platform, sql in PLATFORM_SQL.items():
            # Strip embedding columns for safer fetch; re-attach via cast
            sql_no_emb = sql.replace(",\n            embedding", "")
            # Also handle youtube NULL embedding line
            sql_no_emb = sql_no_emb.replace(",\n            NULL::vector AS embedding", "")
            try:
                df = fetch_platform(cur, platform, sql_no_emb)
            except Exception as e:
                print(f"  WARN {platform} failed: {e}; retrying full SQL")
                try:
                    conn.rollback()
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
                    df = fetch_platform(cur, platform, sql)
                except Exception as e2:
                    print(f"  ERROR {platform}: {e2}")
                    conn.rollback()
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
                    continue

            if df.empty:
                continue

            # Attach embeddings via text cast
            emb_map = {}
            emb_specs = {
                "facebook": ("facebook_posts", "COALESCE(post_id::text, url)"),
                "instagram": ("instagram_posts", "COALESCE(id::text, post_url)"),
                "linkedin": ("linkedin_posts", "post_url"),
                "pinterest": ("pinterest_posts", "COALESCE(pin_id::text, pin_url, id::text)"),
                "reddit": ("reddit_posts", "id::text"),
                "snapchat": ("snapchat_posts", "id::text"),
                "telegram": ("telegram_posts", "COALESCE(post_url, channel_id::text || ':' || message_id::text)"),
                "twitter": ("twitter_posts", "COALESCE(tweet_id::text, id::text)"),
                "whatsapp": ("whatsapp_posts", "COALESCE(channel_id::text || ':' || message_id::text, id::text)"),
                "tiktok": ("tiktok_posts", "post_id::text"),
                "baidu_tieba": ("baidu_tieba_posts", "COALESCE(pid::text, tid::text, id::text)"),
                "baidu_video": ("baidu_video_posts", "id::text"),
                "mastodon": ("mastodon_posts", "id::text"),
                "truthsocial": ("truthsocial_posts", "post_id::text"),
                # "chirpwire": ("chirpwire_posts", "post_id::text"),   # disabled, see PLATFORM_SQL
                # "weibo": ("weibo_posts", "weibo_id::text"),          # disabled, see PLATFORM_SQL
            }
            if platform in emb_specs:
                table, id_expr = emb_specs[platform]
                emb_map = fetch_embeddings_separately(cur, platform, "native_id", table, id_expr)

            facts, _ = normalize_and_dedup(df)
            # Build embeddings from emb_map using post native_id
            emb_rows = []
            for _, row in facts.iterrows():
                key = str(row["native_id"])
                if key in emb_map:
                    vec = emb_map[key]
                    emb_rows.append(
                        {
                            "post_uid": row["post_uid"],
                            "platform": platform,
                            "embedding": vec,
                            "embedding_model": EMBEDDING_MODEL,
                            "embedding_dim": len(vec),
                        }
                    )
            emb_df = pd.DataFrame(emb_rows) if emb_rows else pd.DataFrame(
                columns=["post_uid", "platform", "embedding", "embedding_model", "embedding_dim"]
            )
            print(f"    facts={len(facts):,} embeddings={len(emb_df):,}")
            all_facts.append(facts)
            all_emb.append(emb_df)

        # Comments
        print("  extracting youtube_comments ...", flush=True)
        try:
            cols, rows = q(cur, COMMENT_SQL)
            comment_df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()
            if not comment_df.empty:
                comment_df["posted_at"] = comment_df["posted_at"].map(_as_ts)
                comment_df["scraped_at"] = comment_df["scraped_at"].map(_as_ts)
                comment_df["comment_uid"] = comment_df["comment_id"].map(
                    lambda x: _uid("yt_comment", str(x))
                )
            print(f"    youtube_comments: {len(comment_df):,}")
        except Exception as e:
            print(f"  ERROR comments: {e}")
            conn.rollback()
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")

        # Collection effort
        for name, sql in EFFORT_SQLS.items():
            try:
                cols, rows = q(cur, sql)
                if rows:
                    effort_frames.append(pd.DataFrame(rows, columns=cols))
                    print(f"  effort {name}: {len(rows)} day-rows")
            except Exception as e:
                print(f"  WARN effort {name}: {e}")
                conn.rollback()
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")

        # Explicit rollback (context manager also rolls back)
        conn.rollback()

    # ---- assemble local artifacts ----
    if not all_facts:
        print("ERROR: no posts extracted")
        return 1

    post_facts = pd.concat(all_facts, ignore_index=True)
    # Global dedup in case of overlapping keys (shouldn't happen across platforms)
    post_facts = post_facts.drop_duplicates(subset=["post_uid"], keep="last")

    post_embeddings = (
        pd.concat([e for e in all_emb if not e.empty], ignore_index=True)
        if any(not e.empty for e in all_emb)
        else pd.DataFrame(columns=["post_uid", "platform", "embedding", "embedding_model", "embedding_dim"])
    )
    post_embeddings = post_embeddings.drop_duplicates(subset=["post_uid"], keep="last")

    collection_effort = (
        pd.concat(effort_frames, ignore_index=True) if effort_frames else pd.DataFrame(
            columns=["platform", "day", "rows_ingested", "active_channels", "active_keywords"]
        )
    )

    # Engagement snapshots (append-only)
    snap = post_facts[
        ["post_uid", "likes", "comments", "shares", "views"]
    ].copy()
    snap["extract_id"] = extract_id
    snap["observed_at"] = datetime.now(timezone.utc)
    snap = snap[["post_uid", "extract_id", "observed_at", "likes", "comments", "shares", "views"]]

    extract_meta = pd.DataFrame(
        [
            {
                "extract_id": extract_id,
                "snapshot_ts": snapshot_ts,
                "n_post_facts": len(post_facts),
                "n_comment_facts": len(comment_df),
                "n_embeddings": len(post_embeddings),
                "n_snapshot_rows": len(snap),
                "raw_counts_json": str(raw_counts),
            }
        ]
    )

    print("\n=== writing parquet ===")
    write_parquet(post_facts, "post_facts")
    if not comment_df.empty:
        write_parquet(comment_df, "comment_facts")
    if not post_embeddings.empty:
        # Store embeddings as list columns (pyarrow supports list<float>)
        write_parquet(post_embeddings, "post_embeddings")
    write_parquet(collection_effort, "collection_effort")
    append_parquet(snap, "engagement_snapshots")
    write_parquet(extract_meta, "extract_meta")

    print("=== loading DuckDB ===")
    con = connect(read_only=False)
    replace_table_from_df(con, "post_facts", post_facts)
    if not comment_df.empty:
        replace_table_from_df(con, "comment_facts", comment_df)
    if not post_embeddings.empty:
        # DuckDB: store without giant list for index table; keep parquet as source of truth
        emb_meta = post_embeddings.drop(columns=["embedding"])
        replace_table_from_df(con, "post_embeddings_meta", emb_meta)
    replace_table_from_df(con, "collection_effort", collection_effort)
    replace_table_from_df(con, "extract_meta", extract_meta)

    # Snapshots: union all parquet parts into a table
    snap_glob = str(PARQUET_DIR / "engagement_snapshots" / "*.parquet")
    con.execute("DROP TABLE IF EXISTS engagement_snapshots;")
    con.execute(
        f"CREATE TABLE engagement_snapshots AS SELECT * FROM read_parquet('{snap_glob}', union_by_name=true);"
    )
    register_parquet_views(con)

    # Summary
    print("\n=== summary ===")
    print(f"extract_id     : {extract_id}")
    print(f"duckdb         : {DUCKDB_PATH}")
    by_plat = post_facts.groupby("platform").size().sort_values(ascending=False)
    for plat, n in by_plat.items():
        print(f"  post_facts {plat:16s} {n:>8,}")
    print(f"  post_facts TOTAL      {len(post_facts):>8,}")
    print(f"  embeddings           {len(post_embeddings):>8,}")
    print(f"  comments             {len(comment_df):>8,}")
    print(f"  effort day-rows      {len(collection_effort):>8,}")
    n_snap_extracts = con.execute(
        "SELECT COUNT(DISTINCT extract_id) FROM engagement_snapshots"
    ).fetchone()[0]
    print(f"  snapshot extract_ids {n_snap_extracts}")

    # Sanity checks
    tg = int(by_plat.get("telegram", 0))
    if tg > 20000:
        print(f"WARN: telegram post_facts={tg} still looks undeduped (expected ~14.6k)")
    reddit = post_facts[post_facts["platform"] == "reddit"]
    if not reddit.empty and reddit["has_engagement"].any():
        print("WARN: some reddit rows have has_engagement=True")
    else:
        print("OK: reddit has_engagement all False")

    con.close()
    print("\nLayer 0 complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
