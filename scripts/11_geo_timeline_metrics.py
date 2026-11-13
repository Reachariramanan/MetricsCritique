#!/usr/bin/env python3
"""Geo-ontology timeline metrics + alerts — entity / risk / theme dimensions.

Same statistical substrate as `scripts/07_metrics_v2.py`, applied to three new
grains instead of topic/tag: `entity_uid x day`, `risk_category x day`,
`theme_category x day`. Every number here comes from `scripts/lib/stats_v2.py`
(NB-surprise baselines, Benjamini-Hochberg FDR, AR(1) persistence, causal
Savitzky-Golay acceleration, burst detection, empirical-Bayes shrinkage) — no
statistics are reimplemented in this file.

Reads:
  - `data/analytics.duckdb` (READ-ONLY): `post_facts` for the post_uid -> day
    map and the daily exposure offset (F2, same idea as 07_metrics_v2.py).
  - `data/geo_ontology.duckdb` (READ-ONLY pass): `entity_mentions`, `entity_dim`
    (from `10_entity_extraction.py`), `risk_labels`, `theme_labels`.
Writes (sole writer this stage, opened only for the final publish):
  - `data/geo_ontology.duckdb`: entity_metrics_daily, risk_metrics_daily,
    theme_metrics_daily, geo_alert_candidates.
  - `UI/data/geo_alerts.json`.

Window discipline: identical reasoning to 07_metrics_v2.py PARAMS.window_start.
`post_facts.posted_at` runs back to 2011 (youtube_video historical backfill).
Reindexing any of these series over that full calendar would reproduce the D1
zero-inflation pathology stats_v2's self-checks exist to catch (see
`d1_single_post_not_extreme` etc.) — so, as in Metrics v2, trend/alert columns
are restricted to the live collection window.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR, DUCKDB_PATH, EDA_ROOT
from lib import stats_v2 as S

GEO_DB = DATA_DIR / "geo_ontology.duckdb"
UI_JSON_OUT = EDA_ROOT / "UI" / "data" / "geo_alerts.json"

# ==========================================================================
# PARAMS — mirrors 07_metrics_v2.py defaults so the two alerting substrates
# are directly comparable. Nothing below hardcodes a number outside this block.
# ==========================================================================
PARAMS: dict = {
    "window_start": "2026-06-14",   # same live-collection window as Metrics v2
    "window_end": None,             # None -> max observed posted day
    "baseline_window": 28,
    "baseline_min_days": 14,
    "baseline_min_mean": 0.5,
    "support_min_v": 3,
    "nb_use_exposure_offset": True,
    "fdr_method": "bh",
    "fdr_alpha": 0.05,
    "fdr_scope": "day_global",  # read by apply_fdr_family(); "day" = legacy per-dimension
    "fdr_pvalue_kind": "midp",
    # Severity. Was a bare `q < 0.01` hardcoded at the call site -- evidence
    # strength only, no effect size -- so a 3-post WordPiece fragment
    # (`##M`, V=3 vs mu=0.28, q=0.0054) outranked maritime_security (V=164,
    # q=0.0126), and the dashboard feed sorts on this. "high" now additionally
    # requires the finding to be materially large in absolute and relative terms.
    "severity_q_high": 0.01,
    "severity_min_v_high": 10,       # absolute volume floor for "high"
    "severity_min_ratio_high": 2.0,  # V must be >= this x the baseline mu
    "alpha_count": 1.0,
    "ema_fast": 7,
    "ema_slow": 21,
    "growth_7d_lag": 7,
    "savgol_window": 5,
    "savgol_order": 2,
    "burst_s": 2.0,
    "burst_gamma": 1.0,
    "burst_cusum_h": 5.0,
    "burst_min_days_kleinberg": 21,
    "burst_min_total_kleinberg": 30,
    "burst_min_mean_kleinberg": 0.5,
    "ar1_min_obs": 12,
    "ljung_box_lags": 7,
    "eb_min_parent_n": 30,
    # entity_uid has a long tail of one-off NER hits (many "unresolved_*"
    # provisional uids). Floor below which a daily time series is not built
    # at all -- same rationale, and same value, as tag_min_posts_ts in
    # 07_metrics_v2.py's hashtag grain. risk_category/theme_category have only
    # 9 and 12 members respectively, so no floor is applied there.
    "entity_min_posts_ts": 20,
    "seed": S.RNG_SEED,
}


def log(msg: str) -> None:
    print(f"[11] {msg}", flush=True)


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()


# ==========================================================================
# load
# ==========================================================================
def load_source() -> dict:
    log(f"opening {DUCKDB_PATH} READ-ONLY")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        posts = con.execute(
            "SELECT post_uid, platform, author_uid, posted_at FROM post_facts"
        ).df()
    finally:
        con.close()
    n0 = len(posts)
    posts["posted_at"] = pd.to_datetime(posts["posted_at"], utc=True, errors="coerce")
    posts = posts[posts["posted_at"].notna()].copy()
    posts["day"] = posts["posted_at"].dt.date
    log(f"post_facts: {n0:,} rows, {len(posts):,} with a usable posted_at")

    log(f"opening {GEO_DB} READ-ONLY (pre-existing tables from earlier stages)")
    con = duckdb.connect(str(GEO_DB), read_only=True)
    try:
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        required = {"entity_mentions", "entity_dim", "risk_labels", "theme_labels"}
        missing = required - existing
        assert not missing, f"required upstream tables missing from {GEO_DB}: {missing}"
        entity_mentions = con.execute(
            "SELECT post_uid, entity_uid, entity_type, ner_score, subtype_source "
            "FROM entity_mentions"
        ).df()
        entity_dim = con.execute(
            "SELECT entity_uid, canonical_name, entity_type, entity_subtype, "
            "subtype_source, mention_count, distinct_posts FROM entity_dim"
        ).df()
        risk_labels = con.execute(
            "SELECT post_uid, category, score, gray_zone FROM risk_labels"
        ).df()
        theme_labels = con.execute(
            "SELECT post_uid, category, score, gray_zone FROM theme_labels"
        ).df()
    finally:
        con.close()
    log(f"entity_mentions={len(entity_mentions):,} entity_dim={len(entity_dim):,} "
        f"risk_labels={len(risk_labels):,} theme_labels={len(theme_labels):,}")
    return {
        "posts": posts, "entity_mentions": entity_mentions, "entity_dim": entity_dim,
        "risk_labels": risk_labels, "theme_labels": theme_labels,
    }


# ==========================================================================
# empirical Bayes shrinkage of the per-cell mean confidence score, pooled
# per-key across days (group_key = key_col itself -- the dimension's own
# across-day mean is the parent, exactly the role topic_id plays in
# 07_metrics_v2.py's apply_eb()). Reuses S.estimate_eb_k / S.eb_shrink.
# ==========================================================================
def apply_eb_generic(cells: pd.DataFrame, key_col: str, P: dict) -> dict:
    n = cells["score_n"].astype(float)
    m = cells["score_mean"].astype(float)
    sumv = m * n
    num = float(np.nansum(cells["score_ss"].astype(float) - n * m ** 2))
    den = float(np.nansum(np.where(n > 1, n - 1, 0)))
    within = num / den if den > 0 else np.nan
    if not np.isfinite(within) or within <= 0:
        within = float(np.nanvar(m)) if np.isfinite(np.nanvar(m)) else 1.0
    ok = n > 0
    k, tau2 = S.estimate_eb_k(m[ok].to_numpy(), n[ok].to_numpy(), within)
    glob = float(np.nansum(sumv) / max(np.nansum(n), 1e-9)) if ok.any() else np.nan

    gsum = cells.assign(_s=sumv, _n=n).groupby(key_col, dropna=False)[["_s", "_n"]].sum()
    parents = {
        gid: (row["_s"] / row["_n"] if row["_n"] >= P["eb_min_parent_n"] else glob)
        for gid, row in gsum.iterrows()
    }
    pm = [parents.get(gid, glob) for gid in cells[key_col]]
    cells["score_eb"] = [S.eb_shrink(mm, nn, pp, k) for mm, nn, pp in zip(m, n, pm)]
    return {
        "k": round(float(k), 4),
        "tau2": None if not np.isfinite(tau2) else round(float(tau2), 6),
        "within_var": round(float(within), 6),
        "global_mean": None if not np.isfinite(glob) else round(glob, 6),
    }


# ==========================================================================
# the one production timeline path, shared by all three dimensions
# ==========================================================================
def _author_concentration(df: pd.DataFrame, key_col: str) -> pd.DataFrame:
    """Per (key, day): distinct authors, posts-per-author, and Hurlbert's PIE.

        PIE = (N / (N - 1)) * (1 - sum p_i^2)

    the probability that two posts drawn without replacement come from different
    authors. Undefined at N = 1 (NULL, never 0 -- one post is not "zero
    diversity", it is no evidence about diversity).

    posts_per_author is the syndication ratio: near 1 is broad independent
    pickup, high is one account or one story echoed.
    """
    g = df.groupby([key_col, "day", "author_uid"], dropna=False)["post_uid"] \
          .nunique().reset_index(name="k")
    tot = g.groupby([key_col, "day"], dropna=False)["k"].sum().rename("N")
    g = g.merge(tot, on=[key_col, "day"], how="left")
    g["p2"] = (g["k"] / g["N"]) ** 2
    agg = g.groupby([key_col, "day"], dropna=False).agg(
        n_authors=("author_uid", "nunique"),
        _sum_p2=("p2", "sum"),
        _N=("N", "first"),
    ).reset_index()
    N = agg["_N"].astype(float)
    agg["posts_per_author"] = N / agg["n_authors"].replace(0, np.nan)
    agg["pie_author"] = np.where(
        N > 1, (N / (N - 1)) * (1.0 - agg["_sum_p2"]), np.nan)
    return agg[[key_col, "day", "n_authors", "posts_per_author", "pie_author"]]


def build_dimension_panel(
    long_df: pd.DataFrame,
    key_col: str,
    P: dict,
    *,
    effort_by_day: pd.Series,
    calendar_days: list,
    window_start: date,
    window_end: date,
    min_total_ts: int = 0,
) -> tuple[pd.DataFrame, dict]:
    """`long_df` columns: post_uid, day, <key_col>, score, low_conf (bool).

    One row per (post_uid, key) mention/label hit -- multiple rows per post
    are expected (multi-label themes/risks, multiple entity mentions).
    """
    df = long_df[long_df["day"].between(window_start, window_end)].copy()

    cell = df.groupby([key_col, "day"], dropna=False).agg(
        V=("post_uid", "nunique"),
        score_sum=("score", "sum"),
        score_n=("score", "count"),
        low_conf_sum=("low_conf", "sum"),
        low_conf_n=("low_conf", "count"),
    ).reset_index()
    ss = (
        df.assign(_s2=df["score"].astype(float) ** 2)
        .groupby([key_col, "day"], dropna=False)["_s2"].sum()
        .reset_index(name="score_ss")
    )
    cell = cell.merge(ss, on=[key_col, "day"], how="left")
    cell["score_mean"] = cell["score_sum"] / cell["score_n"].replace(0, np.nan)
    cell["low_conf_share"] = cell["low_conf_sum"] / cell["low_conf_n"].replace(0, np.nan)

    # ---- source concentration: the only volume-orthogonal columns here -----
    # Both borrowed from the sibling GDELT project (docs/metrics-critique.md).
    #
    # pie_author is Hurlbert's PIE, NOT Shannon or Herfindahl. That project
    # measured that the naive concentration measures carry an n-bias in the
    # *opposite* direction -- E[HHI] falls mechanically as N rises, so dyad HHI
    # came out at rho -0.407 against volume: an inverted volume proxy wearing the
    # costume of a diversity metric. PIE's (N/(N-1)) factor removes that bias
    # exactly, and was the single candidate of eight that measured clean (+0.055).
    if "author_uid" in df.columns:
        conc = _author_concentration(df, key_col)
        cell = cell.merge(conc, on=[key_col, "day"], how="left")
    else:
        cell["n_authors"] = np.nan
        cell["posts_per_author"] = np.nan
        cell["pie_author"] = np.nan

    totals = cell.groupby(key_col)["V"].sum()
    keep_keys = set(totals[totals >= min_total_ts].index) if min_total_ts else set(totals.index)
    diag = {
        "n_keys_total": int(totals.size), "n_keys_kept": int(len(keep_keys)),
        "min_total_ts": min_total_ts,
    }
    cell = cell[cell[key_col].isin(keep_keys)].copy()

    out_rows: list[pd.DataFrame] = []
    burst_methods: dict[str, int] = {}
    for key, g in cell.groupby(key_col, dropna=False):
        g = g.set_index("day")
        first = g.index.min()
        cal = [d for d in calendar_days if window_start <= d <= window_end and d >= first]
        if not cal:
            continue
        s = g.reindex(cal)
        s[key_col] = key
        s["V"] = s["V"].fillna(0.0)
        s = s.reset_index().rename(columns={"index": "day"})

        s["effort"] = [float(effort_by_day.get(d, 1.0)) for d in s["day"]]
        s["effort"] = s["effort"].astype(float).clip(lower=1.0)
        s["V_rate"] = s["V"] / s["effort"]
        a = P["alpha_count"]
        s["log_rate"] = np.log(s["V"] + a) - np.log(s["effort"])
        s["log_count"] = np.log(s["V"] + a)

        # ---- growth / momentum -----------------------------------------
        s["growth"] = s["log_rate"].diff()
        L = P["growth_7d_lag"]
        v7 = s["V"].rolling(L, min_periods=1).sum()
        e7 = s["effort"].rolling(L, min_periods=1).sum()
        r7 = np.log(v7 + a) - np.log(e7)
        s["growth_7d"] = r7 - r7.shift(L)
        gz = s["growth"].fillna(0.0)
        s["momentum"] = ema(gz, P["ema_fast"]) - ema(gz, P["ema_slow"])

        # ---- acceleration (causal Savitzky-Golay, no look-ahead) --------
        s["accel"] = s["growth"].diff()
        s["accel_smooth"] = S.causal_savgol_deriv(
            s["log_rate"].to_numpy(dtype=float),
            window=P["savgol_window"], order=P["savgol_order"], deriv=2,
        )

        # ---- NB baseline + surprise (trailing, current-day excluded) ---
        counts = s["V"].to_numpy(dtype=float)
        fits = S.trailing_baseline_fits(
            counts, window=P["baseline_window"],
            min_days=P["baseline_min_days"], min_mean=P["baseline_min_mean"],
        )
        mu_adj, theta, bdays, bmean, dist, p_ex, p_mid = ([] for _ in range(7))
        for t, f in enumerate(fits):
            if f is None or not f.ok:
                mu_adj.append(np.nan); theta.append(np.nan); bdays.append(0)
                bmean.append(np.nan); dist.append(None)
                p_ex.append(np.nan); p_mid.append(np.nan)
                continue
            lo = max(0, t - P["baseline_window"])
            e_base = float(np.nanmean(s["effort"].to_numpy()[lo:t])) if t > lo else np.nan
            scale = 1.0
            if P["nb_use_exposure_offset"] and np.isfinite(e_base) and e_base > 0:
                scale = float(s["effort"].iloc[t]) / e_base
            f_adj = S.NBFit(f.mu * scale, f.theta, f.dist, f.method, f.n, f.var)
            mu_adj.append(f_adj.mu); theta.append(f_adj.theta); bdays.append(f.n)
            bmean.append(f.mu); dist.append(f.dist)
            p_ex.append(S.exceedance_p(counts[t], f_adj, midp=False))
            p_mid.append(S.exceedance_p(counts[t], f_adj, midp=True))
        s["baseline_mu"] = mu_adj
        s["baseline_theta"] = theta
        s["baseline_days"] = bdays
        s["baseline_mean"] = bmean
        s["baseline_dist"] = dist
        s["surprise_p"] = p_ex
        s["_p_fdr"] = p_mid if P["fdr_pvalue_kind"] == "midp" else p_ex
        s["surprise"] = S.surprise_from_p(s["surprise_p"].to_numpy(dtype=float))

        # ---- F3 support gate ---------------------------------------------
        s["support_n"] = s["V"]
        s["support_ok"] = (
            (s["baseline_days"] >= P["baseline_min_days"])
            & (s["baseline_mean"] >= P["baseline_min_mean"])
            & (s["V"] >= P["support_min_v"])
        ).fillna(False)

        # ---- burst (series-level gate, independent of the per-cell F3 gate)
        burst_ok = bool(
            len(s) >= P["baseline_min_days"] and float(np.nanmean(counts)) >= P["baseline_min_mean"]
        )
        s["burst_support_ok"] = burst_ok
        if burst_ok:
            br = S.detect_bursts(
                counts, baseline_mu=s["baseline_mu"].to_numpy(dtype=float),
                min_days_kleinberg=P["burst_min_days_kleinberg"],
                min_total_kleinberg=P["burst_min_total_kleinberg"],
                min_mean_kleinberg=P["burst_min_mean_kleinberg"],
                s=P["burst_s"], gamma=P["burst_gamma"], cusum_h=P["burst_cusum_h"],
            )
            s["burst_state"] = br.state
            s["burst_intensity"] = br.intensity
            s["burst_method"] = br.method
            burst_methods[br.method] = burst_methods.get(br.method, 0) + 1
        else:
            s["burst_state"] = np.nan
            s["burst_intensity"] = np.nan
            s["burst_method"] = None
            burst_methods["ungated_insufficient_support"] = (
                burst_methods.get("ungated_insufficient_support", 0) + 1)

        # ---- AR(1) persistence, fit once per series on the log-rate level
        lr = s["log_rate"].dropna().to_numpy(dtype=float)
        fit = S.fit_ar1(lr, min_obs=P["ar1_min_obs"], lb_lags=P["ljung_box_lags"])
        s["ar1_phi"] = fit.phi
        s["ar1_half_life_days"] = fit.half_life_days
        s["ar1_ljung_box_p"] = fit.ljung_box_p
        s["ar1_n_obs"] = fit.n_obs
        s["ar1_fit_ok"] = fit.ok

        out_rows.append(s)

    panel = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()
    diag["burst_methods"] = burst_methods
    if not len(panel):
        diag["eb"] = {}
        return panel, diag

    # ---- share of the day's total volume (no support gate, scale-free) --
    tot = panel.groupby("day")["V"].transform("sum").replace(0, np.nan)
    panel["share_V"] = panel["V"] / tot

    # ---- support gate blanks the trend/evidence family -------------------
    for c in ["surprise", "surprise_p", "accel", "accel_smooth", "momentum",
              "growth", "growth_7d"]:
        panel.loc[~panel["support_ok"], c] = np.nan

    # ---- FDR (F6) ---------------------------------------------------------
    # fdr_scope == "day_global" defers this to apply_fdr_family(), which pools
    # every dimension into ONE family per day. See that function for why.
    if P.get("fdr_scope") == "day_global":
        panel["surprise_q"] = np.nan
        panel["alert_flag"] = False
    else:
        panel["surprise_q"] = np.nan
        gated = panel["support_ok"] & panel["_p_fdr"].notna()
        for d, idx in panel.loc[gated].groupby("day").groups.items():
            idx = list(idx)
            panel.loc[idx, "surprise_q"] = S.bh_qvalues(
                panel.loc[idx, "_p_fdr"].to_numpy(dtype=float), method=P["fdr_method"]
            )
        panel["alert_flag"] = (panel["surprise_q"] < P["fdr_alpha"]).fillna(False)
        panel = panel.drop(columns=["_p_fdr"])

    diag["eb"] = apply_eb_generic(panel, key_col, P)
    return panel, diag


def apply_fdr_family(panels: dict[str, pd.DataFrame], P: dict) -> dict:
    """One Benjamini-Hochberg family per day, pooled across every dimension.

    The family boundary used to be *structural* rather than chosen: the BH loop
    sat inside build_dimension_panel, so "how many families per day" was exactly
    "how many times that function happens to be called". Three dimensions meant
    three families each spending its own alpha = 0.05, and adding the CAMEO axes
    would have silently taken that to six -- roughly doubling the expected false
    positives while no line of code looked like it changed. That compounds the
    already-documented D12 finding (risk alerts firing on 36% of support_ok
    cells).

    PARAMS["fdr_scope"] was declared but never read, so the params block asserted
    a choice the code did not make. It is now load-bearing:
      "day"        - per (day, dimension), the historical behaviour
      "day_global" - per day across all dimensions (default; strictly stricter)

    Pooling is NOT uniformly conservative, and it would be wrong to sell it that
    way. BH's q depends on a test's *rank* within its family as well as the
    family size (q = p * n / rank), so merging a marginal test into a family that
    contains many strongly-significant ones can raise its rank faster than it
    raises n, and thereby *lower* its q. Measured on this corpus, moving from 3
    families/day to 1 took risk 95 -> 58 alerts but entity 260 -> 266 and theme
    31 -> 34 (total 386 -> 358). The justification for pooling is that one day's
    decisions are one family of decisions -- it is the correct family definition,
    not a uniformly stricter one.
    """
    parts = []
    for name, panel in panels.items():
        if not len(panel) or "_p_fdr" not in panel.columns:
            continue
        g = panel["support_ok"] & panel["_p_fdr"].notna()
        sub = panel.loc[g, ["day", "_p_fdr"]].copy()
        sub["_panel"] = name
        sub["_row"] = sub.index
        parts.append(sub)

    info = {"scope": P.get("fdr_scope"), "n_panels": len(panels), "n_tests": 0,
            "families": 0, "tests_per_family": 0.0}
    if not parts:
        for panel in panels.values():
            if len(panel):
                panel.drop(columns=["_p_fdr"], inplace=True, errors="ignore")
        return info

    allp = pd.concat(parts, ignore_index=True)
    allp["q"] = np.nan
    for d, idx in allp.groupby("day").groups.items():
        idx = list(idx)
        allp.loc[idx, "q"] = S.bh_qvalues(
            allp.loc[idx, "_p_fdr"].to_numpy(dtype=float), method=P["fdr_method"])

    for name, panel in panels.items():
        if not len(panel):
            continue
        m = allp[allp["_panel"] == name]
        if len(m):
            panel.loc[m["_row"].to_numpy(), "surprise_q"] = m["q"].to_numpy()
        panel["alert_flag"] = (panel["surprise_q"] < P["fdr_alpha"]).fillna(False)
        panel.drop(columns=["_p_fdr"], inplace=True, errors="ignore")

    n_days = allp["day"].nunique()
    info.update(n_tests=len(allp), families=n_days,
                tests_per_family=len(allp) / max(1, n_days))
    return info


# ==========================================================================
# main
# ==========================================================================
def main() -> int:
    np.random.seed(PARAMS["seed"])
    P = PARAMS
    t0 = datetime.now(timezone.utc)

    log("running stats_v2 self-checks ...")
    checks = S.run_self_checks()
    failed = [k for k, v in checks.items() if not v]
    assert not failed, f"stats_v2 self-checks FAILED: {failed}"
    log(f"  {len(checks)}/{len(checks)} self-checks passed")

    src = load_source()
    posts = src["posts"]
    window_start = pd.to_datetime(P["window_start"]).date()
    window_end = (pd.to_datetime(P["window_end"]).date() if P["window_end"]
                  else max(posts["day"]))
    log(f"window {window_start} .. {window_end}")

    # author_uid rides along so build_dimension_panel can compute source
    # concentration per cell. Measured motive: maritime_security runs 7.28
    # posts/author and three accounts dominate the labelled corpus, so a
    # "burst" can be one account posting forty times.
    posts_day = posts[["post_uid", "day", "author_uid"]].drop_duplicates("post_uid")
    effort_by_day = (
        posts.groupby("day")["author_uid"].nunique().astype(float).clip(lower=1.0)
    )
    calendar_days = sorted(d for d in posts["day"].unique() if window_start <= d <= window_end)
    log(f"exposure/calendar: {len(calendar_days)} active days in window, "
        f"effort range [{effort_by_day.loc[effort_by_day.index.isin(calendar_days)].min():.0f}, "
        f"{effort_by_day.loc[effort_by_day.index.isin(calendar_days)].max():.0f}] active channels/day")

    # ---------------- entity dimension ---------------------------------
    log("grain 1/3: entity_uid x day")
    em = src["entity_mentions"].merge(posts_day, on="post_uid", how="inner")
    log(f"  entity_mentions joined to a day: {len(em):,}/{len(src['entity_mentions']):,}")
    em = em.rename(columns={"ner_score": "score"})
    em["low_conf"] = em["subtype_source"] == "unresolved"
    entity_panel, entity_diag = build_dimension_panel(
        em, "entity_uid", P, effort_by_day=effort_by_day, calendar_days=calendar_days,
        window_start=window_start, window_end=window_end, min_total_ts=P["entity_min_posts_ts"])
    if len(entity_panel):
        ed = src["entity_dim"][["entity_uid", "canonical_name", "entity_type", "entity_subtype"]]
        entity_panel = entity_panel.merge(ed, on="entity_uid", how="left")
    log(f"  entities: {entity_diag['n_keys_kept']}/{entity_diag['n_keys_total']} pass "
        f">= {P['entity_min_posts_ts']} in-window posts; "
        f"entity_metrics_daily rows={len(entity_panel):,} burst={entity_diag['burst_methods']}")

    # ---------------- risk dimension ------------------------------------
    log("grain 2/3: risk_category x day")
    rl = src["risk_labels"].merge(posts_day, on="post_uid", how="inner")
    log(f"  risk_labels joined to a day: {len(rl):,}/{len(src['risk_labels']):,}")
    rl = rl.rename(columns={"category": "risk_category"})
    rl["low_conf"] = rl["gray_zone"]
    risk_panel, risk_diag = build_dimension_panel(
        rl, "risk_category", P, effort_by_day=effort_by_day, calendar_days=calendar_days,
        window_start=window_start, window_end=window_end, min_total_ts=0)
    log(f"  risk_metrics_daily rows={len(risk_panel):,} burst={risk_diag['burst_methods']}")

    # ---------------- theme dimension -----------------------------------
    log("grain 3/3: theme_category x day")
    tl = src["theme_labels"].merge(posts_day, on="post_uid", how="inner")
    log(f"  theme_labels joined to a day: {len(tl):,}/{len(src['theme_labels']):,}")
    tl = tl.rename(columns={"category": "theme_category"})
    tl["low_conf"] = tl["gray_zone"]
    theme_panel, theme_diag = build_dimension_panel(
        tl, "theme_category", P, effort_by_day=effort_by_day, calendar_days=calendar_days,
        window_start=window_start, window_end=window_end, min_total_ts=0)
    log(f"  theme_metrics_daily rows={len(theme_panel):,} burst={theme_diag['burst_methods']}")

    # ---------------- one FDR family per day, across all dimensions ------
    # Must happen after every panel exists: pooling is the whole point, so it
    # cannot be done inside the per-dimension builder.
    fdr_info = apply_fdr_family(
        {"entity": entity_panel, "risk": risk_panel, "theme": theme_panel}, P)
    if fdr_info["families"]:
        log(f"  FDR scope={fdr_info['scope']}: {fdr_info['n_tests']:,} gated tests in "
            f"{fdr_info['families']} families "
            f"({fdr_info['tests_per_family']:.1f} tests/family across "
            f"{fdr_info['n_panels']} dimensions)")
    for _nm, _pn in (("entity", entity_panel), ("risk", risk_panel), ("theme", theme_panel)):
        if len(_pn):
            log(f"    {_nm}: {int(_pn['alert_flag'].sum()):,} alerts of "
                f"{int(_pn['support_ok'].sum()):,} support_ok cells")

    # ---------------- anti-restatement gate ------------------------------
    # Borrowed from the sibling GDELT project, which used it to kill most of its
    # own proposed metrics before shipping them: correlate every published metric
    # against the raw count it is supposed to add information beyond. A metric
    # tracking volume at |Spearman| > 0.7 is not a second opinion, it is the same
    # opinion in different units. See docs/metrics-critique.md.
    #
    # This runs on every build so a NEW restatement cannot land silently -- the
    # ones already known are declared in KNOWN_RESTATEMENTS with a reason.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from audit_restatement import audit_panels, KNOWN_RESTATEMENTS  # noqa: E402
        _rows, _unexpected = audit_panels(
            {"entity": entity_panel, "risk": risk_panel, "theme": theme_panel})
        _flagged = {f"{m}" for _p, m, _pe, _sp, _n in _rows
                    if np.isfinite(_sp) and abs(_sp) > 0.7}
        log(f"  anti-restatement: {len(_rows)} metric-panel pairs checked, "
            f"{len(_flagged)} above |rho|=0.7 "
            f"({len(_flagged & set(KNOWN_RESTATEMENTS))} declared)")
        if _unexpected:
            for u in _unexpected:
                log(f"    UNDECLARED VOLUME RESTATEMENT: {u}")
            raise SystemExit(
                f"[11] {len(_unexpected)} undeclared volume restatement(s) -- fix the "
                f"metric or declare it in audit_restatement.KNOWN_RESTATEMENTS")
    except ImportError as _e:
        log(f"  anti-restatement: SKIPPED ({_e})")

    # ---------------- rename to the schema contract ---------------------
    def finalise(df: pd.DataFrame, key_col: str, score_prefix: str, low_conf_name: str,
                 extra_head: list[str]) -> pd.DataFrame:
        if not len(df):
            return df
        df = df.rename(columns={
            "score_mean": f"{score_prefix}_mean", "score_eb": f"{score_prefix}_eb",
            "score_n": f"{score_prefix}_n", "low_conf_share": low_conf_name,
        })
        df = df.drop(columns=[c for c in ("score_sum", "score_ss", "low_conf_sum",
                                          "low_conf_n") if c in df.columns])
        head = ["day", key_col] + extra_head + [
            "V", "V_rate", "share_V", "effort",
            "growth", "growth_7d", "momentum", "accel", "accel_smooth",
            "surprise", "surprise_p", "surprise_q", "alert_flag",
            "baseline_mu", "baseline_theta", "baseline_dist", "baseline_days", "baseline_mean",
            "burst_state", "burst_intensity", "burst_method", "burst_support_ok",
            "ar1_phi", "ar1_half_life_days", "ar1_ljung_box_p", "ar1_n_obs", "ar1_fit_ok",
            f"{score_prefix}_mean", f"{score_prefix}_eb", f"{score_prefix}_n", low_conf_name,
            "support_n", "support_ok",
        ]
        head = [c for c in head if c in df.columns]
        return df[head + [c for c in df.columns if c not in head]]

    entity_metrics_daily = finalise(
        entity_panel, "entity_uid", "ner_score", "unresolved_share",
        ["canonical_name", "entity_type", "entity_subtype"])
    risk_metrics_daily = finalise(risk_panel, "risk_category", "avg_score", "gray_zone_share", [])
    theme_metrics_daily = finalise(theme_panel, "theme_category", "avg_score", "gray_zone_share", [])

    # ---------------- assertions before publish --------------------------
    log("post-build assertions")
    for name, df, key_col in (
        ("entity_metrics_daily", entity_metrics_daily, "entity_uid"),
        ("risk_metrics_daily", risk_metrics_daily, "risk_category"),
        ("theme_metrics_daily", theme_metrics_daily, "theme_category"),
    ):
        if not len(df):
            continue
        dup = df.duplicated(subset=[key_col, "day"]).sum()
        assert dup == 0, f"{name} has {dup} duplicate grain keys"
        bad = df[(~df["support_ok"]) & df["surprise"].notna()]
        assert len(bad) == 0, f"{name}: {len(bad)} rows publish surprise without support"
        assert df.loc[df["V"] == 0, "surprise"].notna().sum() == 0, \
            f"{name}: D1 regression -- zero-volume cell carries a surprise value"
    log("  all assertions passed")

    # ---------------- geo_alert_candidates (FDR q<0.05) -------------------
    log("geo_alert_candidates (FDR q<0.05)")
    al = []
    for df, dtype, key_col in (
        (entity_metrics_daily, "entity", "entity_uid"),
        (risk_metrics_daily, "risk", "risk_category"),
        (theme_metrics_daily, "theme", "theme_category"),
    ):
        if not len(df):
            continue
        d = df[df["alert_flag"]]
        for _, r in d.iterrows():
            _mu = float(r["baseline_mu"]) if np.isfinite(r["baseline_mu"]) else np.nan
            _ratio = (float(r["V"]) / _mu) if (np.isfinite(_mu) and _mu > 0) else np.inf
            _is_high = (
                r["surprise_q"] < PARAMS["severity_q_high"]
                and float(r["V"]) >= PARAMS["severity_min_v_high"]
                and _ratio >= PARAMS["severity_min_ratio_high"]
            )
            al.append({
                "day": r["day"], "dimension_type": dtype, "dimension_key": r[key_col],
                "metric": "surprise", "value": float(r["surprise"]),
                "q_value": float(r["surprise_q"]),
                "severity": "high" if _is_high else "medium",
                "rationale": (
                    f"V={r['V']:.0f} vs {r['baseline_dist']} baseline mu="
                    f"{r['baseline_mu']:.2f} over {int(r['baseline_days'])}d "
                    f"(p={r['surprise_p']:.2e}, q={r['surprise_q']:.4f})"
                ),
                "support_n": float(r["support_n"]),
            })
    geo_alert_candidates = pd.DataFrame(al)
    if len(geo_alert_candidates):
        geo_alert_candidates = geo_alert_candidates.sort_values(
            ["day", "q_value"], ascending=[False, True]).reset_index(drop=True)
    log(f"  geo_alert_candidates rows={len(geo_alert_candidates):,}")

    # ---------------- publish to geo_ontology.duckdb (sole writer) -------
    log(f"opening {GEO_DB} read_only=False (sole writer this stage)")
    con = duckdb.connect(str(GEO_DB), read_only=False)
    db_rows: dict[str, int] = {}
    try:
        con.execute("BEGIN TRANSACTION;")
        for name, df in (
            ("entity_metrics_daily", entity_metrics_daily),
            ("risk_metrics_daily", risk_metrics_daily),
            ("theme_metrics_daily", theme_metrics_daily),
            ("geo_alert_candidates", geo_alert_candidates),
        ):
            payload = df if len(df) else pd.DataFrame({"_empty": []})
            con.register("_stage_df", payload)
            con.execute(f'DROP TABLE IF EXISTS "{name}";')
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _stage_df;')
            con.unregister("_stage_df")
            db_rows[name] = con.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0]
        con.execute("COMMIT;")
    except Exception:
        con.execute("ROLLBACK;")
        raise
    finally:
        con.close()
    for name, n in db_rows.items():
        log(f"  published {name:22s} rows={n:>7,}")

    # ---------------- UI/data/geo_alerts.json ------------------------------
    def jsonable(v):
        if v is None:
            return None
        if isinstance(v, (bool, np.bool_)):
            return bool(v)
        if isinstance(v, (int, np.integer)):
            return int(v)
        if isinstance(v, (float, np.floating)):
            return None if not math.isfinite(v) else round(float(v), 6)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    # support_n included so downstream consumers can actually enforce the
    # "never alert on gated cells" rule that docs/ALERTING_KEYING.md requires --
    # notify_poller.py had no field to gate on because it was omitted here.
    fields = ["day", "dimension_type", "dimension_key", "metric", "value",
              "q_value", "severity", "rationale", "support_n"]
    records = [
        {k: jsonable(r[k]) for k in fields}
        for r in geo_alert_candidates.to_dict("records")
    ] if len(geo_alert_candidates) else []
    UI_JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    UI_JSON_OUT.write_text(json.dumps(records, indent=None, separators=(",", ":")))
    log(f"wrote {UI_JSON_OUT} ({len(records):,} alerts)")

    log(f"done in {(datetime.now(timezone.utc) - t0).total_seconds():.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
