#!/usr/bin/env python3
"""
Layer 4 — Compute the 10 signal metrics at (topic × platform × day).

Reads local DuckDB only. Writes metrics_daily + METRICS_SPEC.md.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DUCKDB_PATH, REPORTS_DIR, TS_EXCLUDE_PLATFORMS
from lib.duck import connect, replace_table_from_df, write_parquet

ALPHA = 1.0
EMA_FAST = 7
EMA_SLOW = 21
BASELINE_DAYS = 28


def robust_z(series: pd.Series, baseline: pd.Series) -> float:
    med = baseline.median()
    mad = (baseline - med).abs().median()
    if pd.isna(mad) or mad == 0:
        mad = baseline.std()
        if pd.isna(mad) or mad == 0:
            return 0.0
        # approx convert std to MAD scale
        mad = mad * 0.6745
    x = series.iloc[-1] if len(series) else np.nan
    if pd.isna(x) or pd.isna(med):
        return np.nan
    return float((x - med) / (1.4826 * mad + 1e-9))


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()


def shannon_entropy(counts: np.ndarray) -> float:
    total = counts.sum()
    if total <= 0:
        return 0.0
    p = counts[counts > 0] / total
    return float(-(p * np.log(p + 1e-12)).sum())


def hhi(counts: np.ndarray) -> float:
    total = counts.sum()
    if total <= 0:
        return 1.0
    p = counts / total
    return float((p**2).sum())


def virality_raw(platform: str, likes, comments, shares, views) -> float:
    likes = 0.0 if pd.isna(likes) else float(likes)
    comments = 0.0 if pd.isna(comments) else float(comments)
    shares = 0.0 if pd.isna(shares) else float(shares)
    views = 0.0 if pd.isna(views) else float(views)

    # 1. View-centric platforms
    # NOTE: baidu_video excluded — DB shows 0 shares/likes/comments for all 240 posts;
    # including it would collapse all percentiles to 0.0. Returns NaN below.
    if platform in ("telegram", "snapchat", "tiktok"):
        return shares / (views + ALPHA) if (views or shares) else np.nan

    # 1b. Baidu Video: only view data exists; no interaction signals → not rankable.
    if platform == "baidu_video":
        return np.nan

    # 2. Twitter / X (views-weighted if views exist, else interaction share fallback)
    if platform == "twitter":
        if views > 0:
            return shares / (views + ALPHA)
        d = likes + comments + shares
        return shares / (d + ALPHA) if d else np.nan

    # 3. Microblogging with shares (TruthSocial, Mastodon, Facebook)
    if platform in ("truthsocial", "mastodon", "facebook", "facebook_profile"):
        d = likes + comments + shares
        return shares / (d + ALPHA) if d else np.nan

    # 4. YouTube Video
    if platform == "youtube_video":
        return (likes + comments) / (views + ALPHA) if (views or likes or comments) else np.nan

    # 5. Share-less & Discussion platforms (Instagram, LinkedIn, Baidu Tieba)
    # Uses conversation velocity (comments / likes) to avoid 100% 0.50 percentile collapse
    if platform in ("instagram", "linkedin", "baidu_tieba"):
        return comments / (likes + ALPHA) if (likes or comments) else np.nan

    # 6. YouTube Comments
    if platform == "youtube_comment":
        return likes / (comments + ALPHA) if (likes or comments) else np.nan

    return np.nan


def age_normalise(platform: str, eng: float, age: float, age_params: dict) -> float:
    """Residual of log1p(E) vs fitted age curve → age-normalised engagement."""
    if pd.isna(eng) or eng < 0:
        return np.nan
    p = age_params.get(platform)
    if not p or not p.get("ok"):
        return float(np.log1p(eng))
    age = 0.0 if pd.isna(age) or age < 0 else float(age)
    expected = p["a"] + p["b"] * np.log1p(age)
    return float(np.log1p(eng) - expected)


def fit_half_life(vol_series: pd.Series) -> dict:
    """Post-peak exponential decay half-life on daily volume."""
    v = vol_series.fillna(0).to_numpy(dtype=float)
    if len(v) < 5 or v.max() <= 0:
        return {"half_life_days": np.nan, "lambda": np.nan, "r2": np.nan, "ok": False}
    peak_i = int(np.argmax(v))
    if peak_i >= len(v) - 3:
        return {"half_life_days": np.nan, "lambda": np.nan, "r2": np.nan, "ok": False}
    y = v[peak_i:]
    t = np.arange(len(y), dtype=float)
    # only positive
    mask = y > 0
    if mask.sum() < 4:
        return {"half_life_days": np.nan, "lambda": np.nan, "r2": np.nan, "ok": False}
    # ln(y) = ln(V0) - λ t
    try:
        coef = np.polyfit(t[mask], np.log(y[mask]), 1)
        lam = -float(coef[0])
        if lam <= 0:
            return {"half_life_days": np.nan, "lambda": lam, "r2": np.nan, "ok": False}
        yhat = np.exp(coef[1] + coef[0] * t[mask])
        ss_res = ((y[mask] - yhat) ** 2).sum()
        ss_tot = ((y[mask] - y[mask].mean()) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
        t_half = np.log(2) / lam
        ok = bool(r2 is not None and r2 >= 0.3 and t_half < 365)
        return {"half_life_days": float(t_half), "lambda": lam, "r2": float(r2), "ok": ok}
    except Exception:
        return {"half_life_days": np.nan, "lambda": np.nan, "r2": np.nan, "ok": False}


def main() -> int:
    if not DUCKDB_PATH.exists():
        print("ERROR: run 01_extract_base.py first")
        return 1

    con = connect(read_only=False)

    posts = con.execute("SELECT * FROM post_facts").df()
    posts["posted_at"] = pd.to_datetime(posts["posted_at"], utc=True, errors="coerce")
    posts["posted_day"] = posts["posted_at"].dt.floor("D")

    # Topics
    try:
        post_topic = con.execute("SELECT * FROM post_topic").df()
        topic_dim = con.execute("SELECT * FROM topic_dim").df()
    except Exception:
        print("WARN: post_topic missing — using platform-only pseudo topic")
        post_topic = pd.DataFrame(
            {
                "post_uid": posts["post_uid"],
                "topic_id": "all",
                "confidence": 1.0,
            }
        )
        topic_dim = pd.DataFrame(
            [{"topic_id": "all", "domain": "All", "topic": "all", "size": len(posts)}]
        )

    posts = posts.merge(post_topic[["post_uid", "topic_id", "confidence"]], on="post_uid", how="left")
    posts["topic_id"] = posts["topic_id"].fillna("unassigned")

    # Scores (optional)
    try:
        scores = con.execute("SELECT * FROM post_scores").df()
        posts = posts.merge(
            scores[
                [
                    "post_uid",
                    "tone_score",
                    "tone_valid",
                    "hate_score",
                    "hate_valid",
                    "readability_score",
                    "readability_valid",
                ]
            ],
            on="post_uid",
            how="left",
        )
    except Exception:
        posts["tone_score"] = np.nan
        posts["tone_valid"] = False
        posts["hate_score"] = np.nan
        posts["hate_valid"] = False
        posts["readability_score"] = np.nan
        posts["readability_valid"] = False

    # Age norm params
    age_params = {}
    try:
        ap = con.execute("SELECT * FROM age_norm_params").df()
        for _, r in ap.iterrows():
            age_params[r["platform"]] = {
                "ok": bool(r.get("ok")),
                "a": r.get("a"),
                "b": r.get("b"),
                "r2": r.get("r2"),
            }
    except Exception:
        pass

    # Effort
    try:
        effort = con.execute("SELECT * FROM collection_effort").df()
        effort["day"] = pd.to_datetime(effort["day"], utc=True, errors="coerce").dt.floor("D")
    except Exception:
        effort = pd.DataFrame()

    # Exclude dead platforms from primary TS
    work = posts[~posts["platform"].isin(TS_EXCLUDE_PLATFORMS)].copy()
    work = work.dropna(subset=["posted_day"])

    # Per-post derived fields
    work["E_sum"] = work[["likes", "comments", "shares", "views"]].fillna(0).sum(axis=1)
    work.loc[~work["has_engagement"], "E_sum"] = np.nan
    work["E_age_norm"] = [
        age_normalise(p, e, a, age_params)
        for p, e, a in zip(work["platform"], work["E_sum"], work["scrape_lag_days"])
    ]
    work["virality_raw"] = [
        virality_raw(p, lk, cm, sh, vw)
        for p, lk, cm, sh, vw in zip(
            work["platform"], work["likes"], work["comments"], work["shares"], work["views"]
        )
    ]

    # ---- Daily grain: topic × platform × day ----
    print("aggregating daily panels ...")
    gcols = ["topic_id", "platform", "posted_day"]
    agg = (
        work.groupby(gcols, dropna=False)
        .agg(
            V=("post_uid", "count"),
            E_sum=("E_sum", "sum"),
            E_mean=("E_sum", "mean"),
            E_age_norm_mean=("E_age_norm", "mean"),
            views_sum=("views", "sum"),
            likes_sum=("likes", "sum"),
            comments_sum=("comments", "sum"),
            shares_sum=("shares", "sum"),
            virality_mean=("virality_raw", "mean"),
            n_authors=("author_uid", "nunique"),
            tone_mean=("tone_score", "mean"),
            hate_mean=("hate_score", "mean"),
            hate_prevalence=("hate_score", lambda s: float((s.dropna() >= 0.5).mean()) if s.notna().any() else np.nan),
            readability_mean=("readability_score", "mean"),
            readability_coverage=("readability_valid", "mean"),
            mean_confidence=("confidence", "mean"),
        )
        .reset_index()
        .sort_values(["topic_id", "platform", "posted_day"])
    )

    # Author diversity per cell needs author counts
    def diversity_row(df):
        vc = df["author_uid"].fillna("unknown").value_counts().to_numpy(dtype=float)
        return pd.Series(
            {
                "author_entropy": shannon_entropy(vc),
                "author_1_hhi": 1.0 - hhi(vc),
                "n_authors": len(vc),
            }
        )

    div = (
        work.groupby(gcols, dropna=False)
        .apply(diversity_row, include_groups=False)
        .reset_index()
    )
    agg = agg.drop(columns=["n_authors"], errors="ignore").merge(div, on=gcols, how="left")

    # Global daily volume for share-of-volume
    day_tot = agg.groupby("posted_day")["V"].transform("sum")
    agg["share_V"] = agg["V"] / day_tot.replace(0, np.nan)

    # Platform daily volume for within-platform share
    plat_day_tot = agg.groupby(["platform", "posted_day"])["V"].transform("sum")
    agg["share_V_platform"] = agg["V"] / plat_day_tot.replace(0, np.nan)

    # ---- Per series metrics ----
    rows = []
    half_life_rows = []

    for (topic, plat), g in agg.groupby(["topic_id", "platform"]):
        g = g.sort_values("posted_day").set_index("posted_day")
        # reindex continuous daily range
        if g.index.min() is pd.NaT or g.index.max() is pd.NaT:
            continue
        full_idx = pd.date_range(g.index.min(), g.index.max(), freq="D", tz="UTC")
        g = g.reindex(full_idx)
        g["V"] = g["V"].fillna(0)
        g["share_V"] = g["share_V"].fillna(0)
        g["topic_id"] = topic
        g["platform"] = plat

        # growth
        g["growth"] = np.log((g["V"] + ALPHA) / (g["V"].shift(1) + ALPHA))
        g["growth_share"] = np.log((g["share_V"] + 1e-6) / (g["share_V"].shift(1) + 1e-6))
        # 7d vs prior 7d
        v7 = g["V"].rolling(7, min_periods=1).sum()
        g["growth_7d"] = np.log((v7 + ALPHA) / (v7.shift(7) + ALPHA))

        # momentum
        g["momentum"] = ema(g["growth"].fillna(0), EMA_FAST) - ema(g["growth"].fillna(0), EMA_SLOW)

        # volatility of growth
        g["vol_growth"] = g["growth"].rolling(EMA_SLOW, min_periods=5).std()

        # intensity composite
        g["intensity_raw"] = 0.5 * np.log1p(g["V"]) + 0.5 * np.log1p(g["E_sum"].fillna(0))
        # robust z vs trailing baseline
        intensity_z = []
        tone_z = []
        hate_z = []
        for i in range(len(g)):
            start = max(0, i - BASELINE_DAYS)
            base_i = g["intensity_raw"].iloc[start:i]
            intensity_z.append(
                robust_z(g["intensity_raw"].iloc[start : i + 1], base_i)
                if len(base_i) >= 5
                else np.nan
            )
            base_t = g["tone_mean"].iloc[start:i].dropna()
            if len(base_t) >= 5 and pd.notna(g["tone_mean"].iloc[i]):
                tone_z.append(robust_z(g["tone_mean"].iloc[start : i + 1].dropna(), base_t))
            else:
                tone_z.append(np.nan)
            base_h = g["hate_mean"].iloc[start:i].dropna()
            if len(base_h) >= 5 and pd.notna(g["hate_mean"].iloc[i]):
                hate_z.append(robust_z(g["hate_mean"].iloc[start : i + 1].dropna(), base_h))
            else:
                hate_z.append(np.nan)
        g["intensity_z"] = intensity_z
        g["tone_z"] = tone_z
        g["hate_z"] = hate_z

        # within-platform virality z (per day series)
        vbase = g["virality_mean"]
        g["virality_z"] = (vbase - vbase.rolling(BASELINE_DAYS, min_periods=5).median()) / (
            1.4826 * (vbase - vbase.rolling(BASELINE_DAYS, min_periods=5).median()).abs().rolling(
                BASELINE_DAYS, min_periods=5
            ).median()
            + 1e-9
        )

        # reach proxy
        g["reach_proxy"] = g["views_sum"].where(g["views_sum"].notna() & (g["views_sum"] > 0), g["E_sum"].map(np.log1p))

        # half-life for the whole series (one row per topic×platform)
        hl = fit_half_life(g["V"])
        half_life_rows.append(
            {
                "topic_id": topic,
                "platform": plat,
                **hl,
            }
        )
        g["half_life_days"] = hl["half_life_days"] if hl["ok"] else np.nan
        g["half_life_r2"] = hl.get("r2", np.nan)

        g = g.reset_index().rename(columns={"index": "posted_day"})
        rows.append(g)

    metrics_daily = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    half_life = pd.DataFrame(half_life_rows)

    # Rollups: topic × day (across platforms)
    if not metrics_daily.empty:
        topic_day = (
            metrics_daily.groupby(["topic_id", "posted_day"], dropna=False)
            .agg(
                V=("V", "sum"),
                E_sum=("E_sum", "sum"),
                n_authors=("n_authors", "sum"),
                tone_mean=("tone_mean", "mean"),
                hate_mean=("hate_mean", "mean"),
                reach_proxy=("reach_proxy", "sum"),
                intensity_z=("intensity_z", "mean"),
                growth=("growth", "mean"),
                momentum=("momentum", "mean"),
            )
            .reset_index()
        )
    else:
        topic_day = pd.DataFrame()

    print(f"metrics_daily rows: {len(metrics_daily):,}")
    write_parquet(metrics_daily, "metrics_daily")
    write_parquet(half_life, "half_life")
    if not topic_day.empty:
        write_parquet(topic_day, "metrics_topic_day")
    replace_table_from_df(con, "metrics_daily", metrics_daily)
    replace_table_from_df(con, "half_life", half_life)
    if not topic_day.empty:
        replace_table_from_df(con, "metrics_topic_day", topic_day)

    # ---- METRICS_SPEC.md ----
    lines = []
    lines.append("# Metrics Specification — Layer 4")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Grain: **(topic_id × platform × posted_day)** with rollup to (topic × day).")
    lines.append(f"Smoothing α = {ALPHA}; EMA spans {EMA_FAST}/{EMA_SLOW}; baseline window = {BASELINE_DAYS}d (capped by ~6wk history).")
    lines.append(f"Excluded platforms from TS: {', '.join(sorted(TS_EXCLUDE_PLATFORMS))}")
    lines.append("")
    lines.append("## Formulas")
    lines.append("")
    lines.append("| # | Metric | Formula | Caveats |")
    lines.append("|---|---|---|---|")
    lines.append(
        "| 1 | Growth rate | `g = ln((V_t+α)/(V_{t-1}+α))`; also `growth_share`, `growth_7d` | Report share-based growth to strip collector ramp-up |"
    )
    lines.append(
        f"| 2 | Momentum | `EMA_{EMA_FAST}(g) − EMA_{EMA_SLOW}(g)` | Long window capped at {EMA_SLOW}d |"
    )
    lines.append(
        "| 3 | Half-life | Post-peak fit `V = V_peak·e^{−λ(t−t_peak)}` → `t½=ln2/λ`; require R²≥0.3 | Aggregate only; per-post needs engagement_snapshots accrual |"
    )
    lines.append(
        "| 4 | Intensity | Robust-z of `0.5·log1p(V)+0.5·log1p(E)` vs trailing 28d | Age-norm residuals used in E where fitted |"
    )
    lines.append(
        "| 5 | Tone z-score | Entity-day mean tone vs trailing 28d robust-z | Requires Layer 3; null if unscored |"
    )
    lines.append(
        "| 6 | Virality | Platform amp ratio → within-platform robust-z | Reddit excluded; cross-platform only after standardisation |"
    )
    lines.append(
        "| 7 | Reach / diversity | `reach_proxy = Σviews` else `Σlog1p(E)`; Shannon entropy; `1−HHI`; distinct authors | Reach is a **proxy** — no follower counts |"
    )
    lines.append(
        "| 8 | Readability | Language-appropriate, length-gated mean + coverage | Low coverage on short posts |"
    )
    lines.append(
        "| 9 | Ontology | `topic_id` from Layer 2 | `unassigned` retained |"
    )
    lines.append(
        "| 10 | Hate-speech | Daily mean + prevalence (≥0.5 raw) + robust-z | **Uncalibrated** — war corpus over-fires |"
    )
    lines.append("")
    lines.append("## Output tables")
    lines.append("")
    lines.append("- `metrics_daily` — full panel")
    lines.append("- `metrics_topic_day` — platform rollup")
    lines.append("- `half_life` — one row per topic×platform")
    lines.append("")
    if not metrics_daily.empty:
        lines.append("## Snapshot stats")
        lines.append("")
        lines.append(f"- Rows: {len(metrics_daily):,}")
        lines.append(f"- Topics: {metrics_daily['topic_id'].nunique()}")
        lines.append(f"- Platforms: {', '.join(sorted(metrics_daily['platform'].unique()))}")
        lines.append(
            f"- Date range: {metrics_daily['posted_day'].min()} → {metrics_daily['posted_day'].max()}"
        )
        ok_hl = half_life[half_life.get("ok", False) == True] if "ok" in half_life.columns else half_life.dropna(subset=["half_life_days"])
        lines.append(f"- Half-life fits accepted: {len(ok_hl):,} / {len(half_life):,}")
        lines.append("")
        # top momentum cells recently
        recent = metrics_daily.dropna(subset=["momentum"]).sort_values("posted_day").tail(5000)
        top_m = recent.nlargest(10, "momentum")[
            ["posted_day", "topic_id", "platform", "V", "growth", "momentum", "intensity_z"]
        ]
        lines.append("### Sample: top momentum cells (recent window)")
        lines.append("")
        lines.append("| day | topic | platform | V | growth | momentum | intensity_z |")
        lines.append("|---|---|---|---:|---:|---:|---:|")
        for _, r in top_m.iterrows():
            lines.append(
                f"| {str(r['posted_day'])[:10]} | {r['topic_id']} | {r['platform']} | "
                f"{int(r['V'])} | {r['growth']:.3f} | {r['momentum']:.3f} | "
                f"{'' if pd.isna(r['intensity_z']) else f'{r['intensity_z']:.2f}'} |"
            )
    lines.append("")
    lines.append("## Virality platform formulas")
    lines.append("")
    lines.append("| Platform | Formula |")
    lines.append("|---|---|")
    lines.append("| telegram | forwards / views |")
    lines.append("| instagram, facebook | shares / (likes+comments+shares) |")
    lines.append("| youtube_video | (likes+comments) / views |")
    lines.append("| linkedin | comments / reactions |")
    lines.append("| reddit | excluded (no engagement) |")
    lines.append("")

    out = REPORTS_DIR / "METRICS_SPEC.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")

    summary = {
        "n_metrics_daily": len(metrics_daily),
        "n_topics": int(metrics_daily["topic_id"].nunique()) if len(metrics_daily) else 0,
        "n_half_life_ok": int(half_life["ok"].sum()) if "ok" in half_life.columns else 0,
    }
    (REPORTS_DIR / "metrics_summary.json").write_text(json.dumps(summary, indent=2))
    con.close()
    print("Layer 4 complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
