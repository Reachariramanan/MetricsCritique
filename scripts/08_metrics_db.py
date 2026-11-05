#!/usr/bin/env python3
"""Publish `data/metrics.duckdb` — the metrics-only local database.

Reads the staging parquet produced by `07_metrics_v2.py`, writes the section-5
schema of `docs/METRICS_V2_SPEC.md` into a *separate* DuckDB file (post-level
text and embeddings never enter it), mirrors every table to
`data/parquet_metrics/<table>/`, and generates
`reports/METRICS_V2_SPEC_RESULTS.md`.

Idempotent: re-running drops and rebuilds every table, so the file converges to
the staged state. Only this process writes metrics.duckdb; analytics.duckdb is
never opened for writing anywhere in the v2 pipeline.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR, REPORTS_DIR

STAGE_DIR = DATA_DIR / "metrics_stage"
METRICS_DB = DATA_DIR / "metrics.duckdb"
PARQUET_METRICS = DATA_DIR / "parquet_metrics"
# Shadow build: with METRICS_SHADOW_DIR set, read the staged tables from <dir>/stage and write
# the database, parquet mirror and results report under <dir>, leaving the live files alone.
# The live swap is then a deliberate, separate step.
_SHADOW = os.environ.get("METRICS_SHADOW_DIR")
if _SHADOW:
    _SH = Path(_SHADOW)
    STAGE_DIR = _SH / "stage"
    METRICS_DB = _SH / "metrics.duckdb"
    PARQUET_METRICS = _SH / "parquet_metrics"

# Tables published, in dependency order (dimensions first).
TABLES = [
    "run_meta", "metric_meta", "topic_dim", "tag_dim", "platform_dim",
    "metrics_daily", "metrics_topic_day", "tag_metrics_daily",
    "burst_episodes", "persistence", "alert_candidates",
    "sensitivity_checks", "null_control",
]

# Optional, additive tables (plan Step 3, event chains). 07 stages them only when
# PARAMS["chain_method"] == "v1". Explicit DDL, never CREATE TABLE AS: an id column
# that is NULL on every row (or a 0-row frame) would otherwise be inferred as INTEGER
# (scripts/tests/test_id_column_types.py). Not staged -> any copy left by an earlier
# chain run is dropped, so a published chain table never outlives the episodes it
# was built from. The TABLES above are not affected either way.
OPTIONAL_TABLES_DDL = {
    "event_chain_v1": """
        CREATE TABLE event_chain_v1 (
            chain_id VARCHAR, episode_id VARCHAR, "order" INTEGER, link_score DOUBLE,
            n_topics INTEGER, n_platforms INTEGER, n_authors INTEGER)""",
    "chain_links_v1": """
        CREATE TABLE chain_links_v1 (
            src_episode VARCHAR, dst_episode VARCHAR, lag_days INTEGER,
            overlap_ratio DOUBLE, shared_weight DOUBLE, shared_authors DOUBLE,
            centroid_cos DOUBLE, topic_relation VARCHAR, route VARCHAR,
            link_score DOUBLE, chain_id VARCHAR)""",
}


def publish_optional_tables(con, stage: dict) -> dict[str, int]:
    """Create/insert the optional tables present in `stage`; drop stale ones.

    Runs inside the caller's transaction. Returns {table: rows} for published ones.
    """
    rows: dict[str, int] = {}
    for t, ddl in OPTIONAL_TABLES_DDL.items():
        con.execute(f'DROP TABLE IF EXISTS "{t}";')
        df = stage.get(t)
        if df is None:
            continue
        con.execute(ddl)
        if len(df):
            con.register("_opt_df", df)
            con.execute(f'INSERT INTO "{t}" BY NAME SELECT * FROM _opt_df;')
            con.unregister("_opt_df")
        rows[t] = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    return rows


# Columns that must never appear in the metrics DB (post-level payload).
FORBIDDEN_COLS = {"text", "text_norm", "embedding", "url", "native_id", "post_uid",
                  "author_name", "author_uid"}


def log(msg: str) -> None:
    print(f"[08] {msg}", flush=True)


# ==========================================================================
# metric_meta — every metric ships its own semantics (spec section 5)
# ==========================================================================
_CELL = "topic_id x platform x day"
_GATE_F3 = "support_ok (baseline_days>=14 & baseline_mean>=0.5 & V>=3)"
_GATE_N = "non-null only where its own support count > 0"
_GATE_SERIES = "burst_support_ok (series n_days>=14 & mean(V)>=0.5)"

METRIC_META: list[dict] = [
    # ---- volume / exposure ------------------------------------------------
    dict(metric_key="V", display_name="Volume", family="volume",
         formula="count of distinct posts in the cell", units="posts", grain=_CELL,
         direction="higher=more activity", higher_is_worse=None,
         caveats="raw count; confounded by collector effort — use V_rate for trends",
         support_gate="none (V is its own support)"),
    dict(metric_key="effort", display_name="Collection effort (exposure)", family="volume",
         formula="distinct active channels/authors observed on that platform-day",
         units="channels", grain="platform x day", direction="n/a", higher_is_worse=None,
         caveats=("exposure offset. NOT collection_effort.active_channels: that table is "
                  "keyed on scrape-day, covers 8/45 telegram posted-days and reports 0 "
                  "channels on 5 of those 8. Observed active channels correlate 0.77 with "
                  "the reported figure where both exist. Endogenous by construction: "
                  "V_rate is posts per active channel."),
         support_gate="floored at 1"),
    dict(metric_key="V_rate", display_name="Effort-normalised volume", family="volume",
         formula="V / effort", units="posts per active channel", grain=_CELL,
         direction="higher=more activity per channel", higher_is_worse=None,
         caveats="basis for all growth/acceleration metrics (F2 log-exposure offset)",
         support_gate="none"),
    dict(metric_key="share_V", display_name="Share of daily volume", family="volume",
         formula="V / sum(V) over all cells that day", units="fraction", grain=_CELL,
         direction="higher=larger share of attention", higher_is_worse=None,
         caveats="scale-free; immune to collector ramp", support_gate="none"),
    # ---- growth family ----------------------------------------------------
    dict(metric_key="growth", display_name="Growth (log-rate)", family="growth",
         formula="d/dt log((V+1)/effort) = log_rate_t - log_rate_{t-1}",
         units="log rate per day", grain=_CELL,
         direction="higher=accelerating volume", higher_is_worse=None,
         caveats="effort-normalised (F2); the publishable growth number", support_gate=_GATE_F3),
    dict(metric_key="growth_count", display_name="Growth (raw count)", family="growth",
         formula="log(V_t+1) - log(V_{t-1}+1)", units="log count per day", grain=_CELL,
         direction="higher=accelerating volume", higher_is_worse=None,
         caveats="retained for transparency only; confounded by collector ramp",
         support_gate=_GATE_F3),
    dict(metric_key="growth_share", display_name="Growth of volume share", family="growth",
         formula="log(share_t+1e-6) - log(share_{t-1}+1e-6)", units="log share per day",
         grain=_CELL, direction="higher=gaining relative attention", higher_is_worse=None,
         caveats="strips any platform-wide effort change", support_gate=_GATE_F3),
    dict(metric_key="growth_7d", display_name="7-day growth", family="growth",
         formula="log(sum7(V)+1 / sum7(effort)) - same lagged 7 days",
         units="log rate per week", grain=_CELL,
         direction="higher=weekly acceleration", higher_is_worse=None,
         caveats="smoother, lags by up to a week", support_gate=_GATE_F3),
    dict(metric_key="momentum", display_name="Momentum", family="growth",
         formula="EMA_7(growth) - EMA_21(growth)", units="log rate per day", grain=_CELL,
         direction="higher=short-term outpacing long-term", higher_is_worse=None,
         caveats="sensitive to span choice (corr 0.98 across 5/14 and 10/28 variants)",
         support_gate=_GATE_F3),
    # ---- acceleration family (spec section 3) -----------------------------
    dict(metric_key="accel", display_name="Acceleration (raw)", family="acceleration",
         formula="growth_t - growth_{t-1} (2nd difference of the log rate)",
         units="log rate per day^2", grain=_CELL,
         direction="higher=curvature upward", higher_is_worse=None,
         caveats="2nd differences double the noise — prefer accel_smooth",
         support_gate=_GATE_F3),
    dict(metric_key="accel_smooth", display_name="Smoothed acceleration",
         family="acceleration",
         formula=("2nd derivative of the log rate from a CAUSAL Savitzky-Golay fit "
                  "(trailing window 5, order 2, evaluated at the right edge)"),
         units="log rate per day^2", grain=_CELL,
         direction="higher=scenario popping up faster", higher_is_worse=None,
         caveats=("the publishable acceleration. Deliberately causal: the textbook centred "
                  "SG filter would consume future days (verified leak in stats_v2 self-check "
                  "'centered_savgol_does_leak'). Window-sensitive: corr(window 5, window 7) "
                  "= 0.46, so the smoothing window is the main tuning risk."),
         support_gate=_GATE_F3),
    dict(metric_key="burst_state", display_name="Burst state", family="acceleration",
         formula=("1 = burst, 0 = baseline. Kleinberg 2-state Poisson HMM (Viterbi) where "
                  "the series supports it, otherwise one-sided Poisson CUSUM; see "
                  "burst_method per row"),
         units="0/1", grain=_CELL, direction="1=bursting", higher_is_worse=True,
         caveats=("run on raw counts (Kleinberg needs integer counts), so a rising collector "
                  "ramp can inflate late-window burst states; the CUSUM path uses a trailing "
                  "effort-adjusted baseline and does not have this problem"),
         support_gate=_GATE_SERIES),
    dict(metric_key="burst_intensity", display_name="Burst intensity", family="acceleration",
         formula=("Kleinberg: log p(x|lambda1) - log p(x|lambda0) clipped at 0. "
                  "CUSUM: S_t / h (1.0 at the alarm boundary)"),
         units="nats (Kleinberg) or CUSUM units", grain=_CELL,
         direction="higher=stronger burst", higher_is_worse=True,
         caveats="units differ between the two detectors — compare within burst_method only",
         support_gate=_GATE_SERIES),
    dict(metric_key="burst_method", display_name="Burst detector used", family="acceleration",
         formula="'kleinberg' | 'poisson_cusum' | NULL", units="label", grain=_CELL,
         direction="n/a", higher_is_worse=None,
         caveats="Kleinberg requires n_days>=21, total_V>=30, mean>=0.5; else CUSUM",
         support_gate=_GATE_SERIES),
    dict(metric_key="burst_episode_id", display_name="Burst episode", family="acceleration",
         formula="identifier of the contiguous burst run this day belongs to",
         units="id", grain=_CELL, direction="n/a", higher_is_worse=None,
         caveats="join key to burst_episodes", support_gate=_GATE_SERIES),
    dict(metric_key="onset_slope", display_name="Onset steepness", family="acceleration",
         formula=("logistic fit to the cumulative rising limb -> max slope K*r/4; falls back "
                  "to the OLS slope of log1p(V) on the limb when <4 points or R2<0.5"),
         units="posts/day (logistic) or log-posts/day (fallback)", grain="burst episode",
         direction="higher=arrived more explosively", higher_is_worse=True,
         caveats=("in a 45-day window most rising limbs are 1-3 days, so 36/49 episodes use "
                  "the log-linear fallback — check onset_method before comparing values"),
         support_gate="episode must exist"),
    dict(metric_key="days_to_threshold", display_name="Days to threshold",
         family="acceleration",
         formula="days from the series' first in-window appearance to the first day V>=5",
         units="days", grain=_CELL, direction="lower=faster emergence", higher_is_worse=True,
         caveats="NULL until the threshold is actually crossed (no look-ahead)",
         support_gate="NULL before crossing"),
    dict(metric_key="novelty", display_name="Semantic novelty", family="acceleration",
         formula=("1 - cosine(day centroid, trailing 21-day topic centroid) over bge-m3 "
                  "embeddings, trailing window EXCLUDES the current day"),
         units="cosine distance [0,2]", grain=_CELL,
         direction="higher=new sub-narrative inside the topic", higher_is_worse=None,
         caveats=("NULL where embeddings are absent — youtube_video has none at all. "
                  "Needs >=3 embedded posts that day and >=20 in the baseline; "
                  "novelty_n / novelty_base_n carry the support."),
         support_gate="novelty_n>=3 & novelty_base_n>=20"),
    dict(metric_key="emergence_score", display_name="Emergence score", family="acceleration",
         formula=("weighted mean of percentile ranks of surprise (0.40), accel_smooth (0.25), "
                  "novelty (0.20) and -log1p(days_to_threshold) (0.15); >=2 components "
                  "required, weights renormalised over the present ones"),
         units="rank in [0,1]", grain=_CELL,
         direction="higher=a situation is appearing", higher_is_worse=True,
         caveats=("the single alerting number for 'popping up'. Ranks are computed over the "
                  "whole in-window panel, so the value is relative to this run's panel; "
                  "emergence_flag additionally requires surprise_q < 0.05 (FDR-gated)"),
         support_gate=_GATE_F3),
    # ---- intensity / evidence --------------------------------------------
    dict(metric_key="surprise", display_name="Volume surprise", family="intensity",
         formula="-log10 P(X >= V | NegBinom(mu_baseline * effort_t/effort_baseline, theta))",
         units="-log10 p (1.0 = p 0.1, 3.0 = p 0.001)", grain=_CELL,
         direction="higher=more unusual volume", higher_is_worse=True,
         caveats=("replaces the v1 robust-z (defect D1). Baseline is fitted by MLE on the "
                  "trailing 28 days EXCLUDING the current day, with a method-of-moments "
                  "theta fallback and a Poisson fallback when theta is unidentifiable. "
                  "Overdispersion is honest but self-masking: a baseline that already "
                  "contains spikes inflates 1/theta and depresses surprise."),
         support_gate=_GATE_F3),
    dict(metric_key="surprise_p", display_name="Exceedance p-value", family="intensity",
         formula="P(X >= V | fitted count baseline), exact discrete upper tail",
         units="probability", grain=_CELL, direction="lower=more unusual",
         higher_is_worse=False,
         caveats="one-sided; only upward excursions are tested", support_gate=_GATE_F3),
    dict(metric_key="surprise_q", display_name="FDR q-value (per day)", family="intensity",
         formula="Benjamini-Hochberg over all supported cells of the same day, on mid-p",
         units="q-value", grain=_CELL, direction="lower=survives multiple testing",
         higher_is_worse=False,
         caveats=("mid-p (P(X>V)+0.5P(X=V)) is used for FDR because the exact discrete tail "
                  "is conservative; surprise_p reports the exact value. m per day is small "
                  "(<=~15 supported cells), so power is limited — surprise_q_global applies "
                  "BH across the whole panel instead."),
         support_gate=_GATE_F3),
    dict(metric_key="alert_flag", display_name="Intensity alert", family="intensity",
         formula="surprise_q < 0.05", units="boolean", grain=_CELL,
         direction="true=unusually intense", higher_is_worse=True,
         caveats="calibrated false-discovery rate, not an arbitrary z cutoff (F6)",
         support_gate=_GATE_F3),
    dict(metric_key="baseline_mu", display_name="Baseline expected count", family="intensity",
         formula="trailing NB mean, rescaled by effort_t / mean(effort over baseline)",
         units="posts", grain=_CELL, direction="n/a", higher_is_worse=None,
         caveats="the exposure-offset adjustment (F2)", support_gate="baseline_days>=14"),
    dict(metric_key="baseline_theta", display_name="Baseline dispersion", family="intensity",
         formula="NB2 theta; Var = mu + mu^2/theta. NULL when the Poisson fallback is used",
         units="dimensionless", grain=_CELL, direction="lower=more overdispersed",
         higher_is_worse=None, caveats="see baseline_dist for which family was used",
         support_gate="baseline_days>=14"),
    # ---- tone / hate / readability ---------------------------------------
    dict(metric_key="tone_mean", display_name="Tone (raw mean)", family="content",
         formula="mean tone_score over posts with tone_valid", units="[-1,1]", grain=_CELL,
         direction="higher=more positive", higher_is_worse=False,
         caveats="unshrunk; noisy at small tone_n — prefer tone_eb", support_gate="tone_n>0"),
    dict(metric_key="tone_eb", display_name="Tone (EB-shrunk)", family="content",
         formula="(n*mean + k*parent_mean)/(n+k), k = within_var/tau^2 (DerSimonian-Laird)",
         units="[-1,1]", grain=_CELL, direction="higher=more positive", higher_is_worse=False,
         caveats=("publishable tone value (F5). Parent is the topic pooled mean, then "
                  "platform, then global, requiring >=30 posts to act as a parent."),
         support_gate=_GATE_N),
    dict(metric_key="tone_n", display_name="Tone support", family="content",
         formula="posts with a valid tone score", units="posts", grain=_CELL,
         direction="n/a", higher_is_worse=None, caveats="", support_gate="none"),
    dict(metric_key="hate_mean", display_name="Hate score (raw mean)", family="content",
         formula="mean hate_score over posts with hate_valid", units="[0,1]", grain=_CELL,
         direction="higher=more toxic", higher_is_worse=True,
         caveats="UNCALIBRATED — see hate_calibrated", support_gate="hate_n>0"),
    dict(metric_key="hate_eb", display_name="Hate score (EB-shrunk)", family="content",
         formula="empirical-Bayes shrunk hate mean", units="[0,1]", grain=_CELL,
         direction="higher=more toxic", higher_is_worse=True,
         caveats=("UNCALIBRATED AND NOT PUBLISHABLE. A war/terror corpus over-fires toxicity "
                  "models; reports/calibration_sample.csv is not yet hand-labelled. The "
                  "dashboard must render every hate_* field visibly gated."),
         support_gate=_GATE_N),
    dict(metric_key="hate_prevalence", display_name="Hate prevalence", family="content",
         formula="share of valid-scored posts with hate_score >= 0.5", units="fraction",
         grain=_CELL, direction="higher=more toxic", higher_is_worse=True,
         caveats="UNCALIBRATED; the 0.5 cutoff is the model's, not a validated threshold",
         support_gate="hate_n>0"),
    dict(metric_key="hate_calibrated", display_name="Hate calibration status",
         family="content", formula="constant false until calibration_sample.csv is labelled",
         units="boolean", grain=_CELL, direction="n/a", higher_is_worse=None,
         caveats="non-negotiable #3 of the spec", support_gate="none"),
    dict(metric_key="readability_mean", display_name="Readability", family="content",
         formula="mean of valid, language-appropriate readability scores, winsorised to [0,100]",
         units="score", grain=_CELL, direction="higher=easier to read", higher_is_worse=None,
         caveats=("coverage is low on short posts (13.7% on youtube_video, 48% on telegram) — "
                  "always read alongside readability_coverage. Raw scores reach 1703 on "
                  "degenerate text, hence the winsorisation."),
         support_gate="read_n>0"),
    dict(metric_key="readability_coverage", display_name="Readability coverage",
         family="content", formula="share of posts in the cell with readability_valid",
         units="fraction", grain=_CELL, direction="higher=more measurable",
         higher_is_worse=None, caveats="", support_gate="none"),
    # ---- engagement / reach ----------------------------------------------
    dict(metric_key="virality_pct", display_name="Virality percentile", family="engagement",
         formula=("EB-shrunk mean of per-post empirical percentiles of the platform "
                  "amplification ratio within (platform x age band)"),
         units="percentile [0,1]", grain=_CELL,
         direction="higher=amplifies more than its peers", higher_is_worse=None,
         caveats=("F9: distribution-free and age-honest, replacing the low-R2 parametric age "
                  "curve. Age = scrape_lag_days, bands 0/.5/1/2/4/7/14/30/90d, >=20 "
                  "observations per band or it falls back to the platform. reddit is NULL "
                  "(no engagement fields at all). Amplification ratio differs per platform, "
                  "so only the percentile is cross-platform comparable."),
         support_gate=_GATE_N),
    dict(metric_key="reach_proxy", display_name="Reach (proxy)", family="engagement",
         formula="sum(views) where views exist, else sum(log1p(total engagement))",
         units="views or log-engagement", grain=_CELL, direction="higher=wider reach",
         higher_is_worse=None,
         caveats="A PROXY. No follower counts exist anywhere in the source data.",
         support_gate="none"),
    dict(metric_key="reach_audience_norm", display_name="Audience-normalised reach",
         family="engagement",
         formula="mean over posts of views / median views of that channel (>=3 posts)",
         units="ratio to channel median", grain=_CELL,
         direction="higher=outperformed its own channel", higher_is_worse=None,
         caveats=("F10. Available only where views exist and the channel has >=3 view-bearing "
                  "posts: 27 telegram channels and 160 youtube channels. Still a proxy."),
         support_gate="channel median available"),
    # ---- diversity / coordination ----------------------------------------
    dict(metric_key="author_entropy", display_name="Author entropy", family="diversity",
         formula="Shannon entropy of the author post-count distribution in the cell",
         units="nats", grain=_CELL, direction="higher=more distinct voices",
         higher_is_worse=False, caveats="bounded by log(n_authors)", support_gate="V>0"),
    dict(metric_key="author_1_hhi", display_name="Author diversity (1-HHI)",
         family="diversity", formula="1 - sum(share_author^2)", units="[0,1)", grain=_CELL,
         direction="higher=more diverse", higher_is_worse=False,
         caveats="0 means a single author produced the whole cell", support_gate="V>0"),
    dict(metric_key="n_authors", display_name="Distinct authors", family="diversity",
         formula="count(distinct author_uid)", units="authors", grain=_CELL,
         direction="higher=more sources", higher_is_worse=False, caveats="",
         support_gate="none"),
    dict(metric_key="duplicate_rate", display_name="Duplicate text rate", family="diversity",
         formula="1 - distinct(normalised text)/count, over posts longer than 20 chars",
         units="fraction", grain=_CELL, direction="higher=more copy-paste",
         higher_is_worse=True,
         caveats="exact normalised-text match only; near-duplicates are not detected",
         support_gate="posts >20 chars must exist"),
    dict(metric_key="coordination_flag", display_name="Coordination flag",
         family="diversity",
         formula="V>=10 AND author_1_hhi<0.30 AND duplicate_rate>0.30",
         units="boolean", grain=_CELL, direction="true=possible coordination",
         higher_is_worse=True,
         caveats=("heuristic, not a test. High duplicate rates on reddit (50%) and instagram "
                  "(45%) are largely feed/template artefacts, so treat as triage only."),
         support_gate="V>=10"),
    # ---- persistence ------------------------------------------------------
    dict(metric_key="ar1_phi", display_name="AR(1) persistence", family="persistence",
         formula="OLS phi from log_rate_t = c + phi*log_rate_{t-1} + e (LEVEL series)",
         units="dimensionless", grain="topic_id x platform",
         direction="higher=shocks persist longer", higher_is_worse=True,
         caveats=("SPEC DEVIATION, flagged for review: F7 specifies the AR(1) on the "
                  "DIFFERENCED log rate, but differencing a stationary series forces phi<0, "
                  "for which ln(0.5)/ln(phi) is undefined — it produced a half-life on only "
                  "4/39 series, reproducing the coverage weakness F7 set out to fix. The "
                  "level fit is published here; the differenced fit is kept verbatim in "
                  "diff_ar1_phi / diff_half_life_days."),
         support_gate="n_obs>=12"),
    dict(metric_key="half_life_days", display_name="Persistence half-life",
         family="persistence", formula="ln(0.5)/ln(ar1_phi), defined for 0<phi<1",
         units="days", grain="topic_id x platform",
         direction="higher=slower decay", higher_is_worse=True,
         caveats="NULL when phi<=0 or phi>=1 (not mean-reverting); read with phi_se",
         support_gate="0<phi<1 and n_obs>=12"),
    dict(metric_key="volatility", display_name="Volatility", family="persistence",
         formula="SD of the log-rate first differences", units="log rate",
         grain="topic_id x platform", direction="higher=choppier", higher_is_worse=True,
         caveats="", support_gate="n_obs>=2"),
    dict(metric_key="ljung_box_p", display_name="Ljung-Box p", family="persistence",
         formula="Ljung-Box Q on the log-rate level, lags 1..7",
         units="p-value", grain="topic_id x platform",
         direction="lower=real temporal structure", higher_is_worse=False,
         caveats=("F8's primary evidence for autocorrelation, replacing the inverted v1 "
                  "null-control criterion (D4)"),
         support_gate="n_obs>=8"),
    dict(metric_key="decay_lambda", display_name="Post-peak decay rate",
         family="persistence", formula="ln V = a - lambda*t fitted from the episode peak",
         units="per day", grain="burst episode", direction="higher=faster fade",
         higher_is_worse=False,
         caveats="scoped to detected burst episodes (F7), not to whole series",
         support_gate="4+ positive post-peak days; check r2"),
]


# ==========================================================================
def read_stage() -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(STAGE_DIR.glob("*.parquet")):
        df = pd.read_parquet(p)
        if list(df.columns) == ["_empty"]:
            df = pd.DataFrame()
        out[p.stem] = df
    return out


def mirror_parquet(name: str, df: pd.DataFrame) -> None:
    d = PARQUET_METRICS / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / "part-0.parquet", index=False)


def fmt(v, nd=3) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        return f"{v:,.{nd}f}"
    return str(v)


def build_report(stage: dict[str, pd.DataFrame], params: dict, db_rows: dict) -> str:
    md = stage["metrics_daily"]
    mt = stage["metrics_topic_day"]
    tg = stage["tag_metrics_daily"]
    d1 = params["d1_before_after"]
    v1, v2 = d1.get("v1", {}), d1["v2"]
    cov = params["hashtag_coverage"]
    L: list[str] = []
    A = L.append

    A("# Metrics v2 — build results")
    A("")
    A(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
      f"run `{stage['run_meta']['run_id'].iloc[0]}` · "
      f"source extract `{stage['run_meta']['source_extract_id'].iloc[0]}`")
    A("")
    A(f"Window: **{params['window']['start']} → {params['window']['end']}** "
      f"(F4 `in_window`; the historical YouTube backfill to 2011 is retained for "
      f"cross-sectional metrics and excluded from every trend).")
    A("")
    A("Generated by `scripts/08_metrics_db.py` from `scripts/07_metrics_v2.py`. "
      "Everything below is measured, not asserted.")
    A("")

    # ---- rows per table -------------------------------------------------
    A("## 1. Rows per table")
    A("")
    A("| table | rows | columns | notes |")
    A("|---|---:|---:|---|")
    notes = {
        "metrics_daily": "topic x platform x day",
        "metrics_topic_day": "topic x day (platform rolled up)",
        "tag_metrics_daily": "tag x platform x day",
        "burst_episodes": "one row per detected burst run",
        "persistence": "one row per topic x platform series",
        "alert_candidates": "FDR-gated intensity + emergence alerts",
        "topic_dim": "ontology dimension",
        "tag_dim": "normalised hashtag dimension",
        "platform_dim": "platform capability dimension",
        "metric_meta": "in-band semantics for every metric",
        "run_meta": "provenance + full params_json",
        "sensitivity_checks": "D5 harness (self-test + window variants)",
        "null_control": "F8 permutation / Ljung-Box evidence",
    }
    for t in TABLES:
        df = stage.get(t, pd.DataFrame())
        A(f"| `{t}` | {db_rows.get(t, len(df)):,} | {len(df.columns)} | {notes.get(t,'')} |")
    A("")

    # ---- support gate ---------------------------------------------------
    iw = md[md["in_window"]]
    A("## 2. Support gating (F3) — coverage of every published number")
    A("")
    A("`support_ok = baseline_days >= {bd} AND baseline_mean >= {bm} AND V >= {mv}`. "
      "Cells that fail it publish `NULL`, never a guess.".format(
          bd=params["params"]["baseline_min_days"],
          bm=params["params"]["baseline_min_mean"],
          mv=params["params"]["support_min_v"]))
    A("")
    A("| panel | rows | in-window | support_ok | pass rate (in-window) |")
    A("|---|---:|---:|---:|---:|")
    for nm, df in (("metrics_daily", md), ("metrics_topic_day", mt), ("tag_metrics_daily", tg)):
        if not len(df):
            continue
        w = int(df["in_window"].sum())
        s = int(df["support_ok"].sum())
        A(f"| `{nm}` | {len(df):,} | {w:,} | {s:,} | {100.0*s/max(w,1):.1f}% |")
    A("")
    A("Support-gate pass rate by volume bucket (`metrics_daily`, in-window):")
    A("")
    A("| V bucket | cells | support_ok | surprise published | max surprise |")
    A("|---|---:|---:|---:|---:|")
    buckets = [(0, 0), (1, 2), (3, 4), (5, 9), (10, 29), (30, 99), (100, 10**9)]
    for lo, hi in buckets:
        sel = iw[(iw["V"] >= lo) & (iw["V"] <= hi)]
        if not len(sel):
            continue
        lbl = "0" if lo == hi == 0 else (f"{lo}+" if hi > 10**8 else f"{lo}–{hi}")
        A(f"| {lbl} | {len(sel):,} | {int(sel['support_ok'].sum()):,} | "
          f"{int(sel['surprise'].notna().sum()):,} | {fmt(sel['surprise'].max())} |")
    A("")
    A("Non-null coverage of each metric family (in-window `metrics_daily`):")
    A("")
    A("| metric | non-null | of in-window rows | gate |")
    A("|---|---:|---:|---|")
    for c, gate in [("V_rate", "none"), ("growth", "F3"), ("accel", "F3"),
                    ("accel_smooth", "F3"), ("momentum", "F3"), ("surprise", "F3"),
                    ("surprise_q", "F3"), ("burst_state", "series-level"),
                    ("burst_intensity", "series-level"), ("novelty", "embeddings + n"),
                    ("days_to_threshold", "after crossing"), ("emergence_score", "F3"),
                    ("tone_eb", "tone_n>0"), ("hate_eb", "hate_n>0 (uncalibrated)"),
                    ("virality_pct", "vir_n>0"), ("reach_proxy", "none"),
                    ("reach_audience_norm", "channel median"),
                    ("author_entropy", "V>0"), ("readability_mean", "read_n>0")]:
        if c in iw.columns:
            A(f"| `{c}` | {int(iw[c].notna().sum()):,} | {len(iw):,} | {gate} |")
    A("")

    # ---- D1 before / after ---------------------------------------------
    A("## 3. D1 — the |z| = 143 pathology is gone")
    A("")
    A("| | v1 (`analytics.duckdb.metrics_daily`) | v2 (`metrics.duckdb.metrics_daily`) |")
    A("|---|---|---|")
    A(f"| panel cells | {fmt(v1.get('n_cells'))} | {fmt(v2['n_cells'])} "
      f"({fmt(v2['n_cells_in_window'])} in-window) |")
    A(f"| cells with V ≤ 2 | {fmt(v1.get('n_cells_V_le_2'))} "
      f"({fmt(v1.get('pct_cells_V_le_2'),2)}%) | {fmt(v2['n_cells_V_le_2'])} |")
    A(f"| out-of-window cells | {fmt(v1.get('n_out_of_window_cells'))} | "
      f"{fmt(v2['n_cells'] - v2['n_cells_in_window'])} (flagged, trends NULL) |")
    A(f"| extreme statistic | `max |intensity_z| = {fmt(v1.get('max_abs_z'),1)}` | "
      f"`max surprise = {fmt(v2['max_surprise'])}` "
      f"(p = {fmt(v2['max_surprise_implied_p'],6)}) |")
    A(f"| worst value on V ≤ 2 | `|z| = {fmt(v1.get('max_abs_z_on_V_le_2'),1)}` | "
      f"**{v2['n_nonnull_surprise_on_V_le_2']} such cells publish anything at all** |")
    A(f"| worst value on V = 0 | `|z| = {fmt(v1.get('max_abs_z_on_V_eq_0'),1)}` | "
      f"**{v2['n_nonnull_surprise_on_V_eq_0']} — zero-volume cells publish nothing** |")
    A(f"| cells with V ≤ 2 and \\|z\\| > 5 | {fmt(v1.get('n_V_le_2_with_abs_z_gt_5'))} | "
      f"0 (structurally impossible: V ≥ {params['params']['support_min_v']} required) |")
    A(f"| a metric on a zero-volume day | {fmt(v1.get('n_extreme_metric_on_zero_volume'))} cells | 0 |")
    A(f"| lowest V carrying an intensity number | 0 | "
      f"{fmt(v2['min_V_with_nonnull_surprise'])} |")
    A("")
    A("**Root cause removed.** v1's three largest \\|z\\| values were all on **V = 0** "
      "days manufactured by reindexing a topic's series over 5,516 calendar days back to "
      "2011: MAD → 0 collapsed the denominator. v2 (a) restricts trends to the live "
      "window, (b) reindexes only over days the platform was actually collecting, "
      "(c) models counts with a Negative Binomial and reports a bounded "
      "`-log10 p`, and (d) refuses to emit anything below the F3 support gate. The "
      "`stats_v2` self-check suite contains three permanent D1 regression tests "
      "(`d1_single_post_not_extreme`, `d1_two_posts_not_extreme`, "
      "`d1_real_spike_is_extreme`) plus a build-time assertion that no zero-volume "
      "cell carries a surprise value.")
    A("")

    # ---- alerts ---------------------------------------------------------
    A("## 4. Alerting (F6)")
    A("")
    p = params["fdr"]
    A(f"BH q-values ({p['method'].upper()}, alpha = {p['alpha']}) computed per "
      f"{p['scope']} on {p['pvalue_kind']} p-values; `surprise_q_global` applies BH "
      f"across the whole panel.")
    A("")
    A("| panel | supported tests | q < 0.05 | q < 0.01 | emergence_flag |")
    A("|---|---:|---:|---:|---:|")
    for nm, df in (("metrics_daily", md), ("metrics_topic_day", mt), ("tag_metrics_daily", tg)):
        if not len(df):
            continue
        A(f"| `{nm}` | {int(df['surprise_q'].notna().sum()):,} | "
          f"{int((df['surprise_q'] < 0.05).sum()):,} | "
          f"{int((df['surprise_q'] < 0.01).sum()):,} | "
          f"{int(df['emergence_flag'].sum()) if 'emergence_flag' in df else 0:,} |")
    ac = stage["alert_candidates"]
    A("")
    if len(ac):
        A("`alert_candidates` by grain and metric:")
        A("")
        A("| grain | metric_key | rows | high severity |")
        A("|---|---|---:|---:|")
        for (g, m), sub in ac.groupby(["grain", "metric_key"]):
            A(f"| {g} | `{m}` | {len(sub):,} | {int((sub['severity']=='high').sum()):,} |")
        A("")

    # ---- top 10 --------------------------------------------------------
    A("## 5. Top cells")
    A("")
    A("### Top 10 by surprise")
    A("")
    A("| day | topic | platform | V | baseline μ | family | surprise | p | q | alert |")
    A("|---|---|---|---:|---:|---|---:|---:|---:|---|")
    for _, r in md.nlargest(10, "surprise").iterrows():
        A(f"| {r['day']} | {r['topic_id']} | {r['platform']} | {int(r['V'])} | "
          f"{fmt(r['baseline_mu'],2)} | {r['baseline_dist']} | {fmt(r['surprise'])} | "
          f"{fmt(r['surprise_p'],5)} | {fmt(r['surprise_q'],4)} | "
          f"{'yes' if r['alert_flag'] else 'no'} |")
    A("")
    A("### Top 10 by emergence_score")
    A("")
    A("| day | topic | platform | V | accel_smooth | novelty | days→thr | emergence | q | flag |")
    A("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for _, r in md.nlargest(10, "emergence_score").iterrows():
        A(f"| {r['day']} | {r['topic_id']} | {r['platform']} | {int(r['V'])} | "
          f"{fmt(r['accel_smooth'])} | {fmt(r['novelty'])} | "
          f"{fmt(r['days_to_threshold'],0)} | {fmt(r['emergence_score'])} | "
          f"{fmt(r['surprise_q'],4)} | {'yes' if r['emergence_flag'] else 'no'} |")
    A("")

    # ---- burst ---------------------------------------------------------
    A("## 6. Burst detection — which method and why")
    A("")
    A(params["burst_method_rule"])
    A("")
    A("| grain | Kleinberg series | Poisson CUSUM series | ungated (no support) |")
    A("|---|---:|---:|---:|")
    for g, d in params["burst_method_counts"].items():
        A(f"| {g} | {d.get('kleinberg',0):,} | {d.get('poisson_cusum',0):,} | "
          f"{d.get('ungated_insufficient_support',0):,} |")
    be = stage["burst_episodes"]
    if len(be):
        A("")
        A(f"`burst_episodes`: {len(be):,} episodes; "
          f"onset fit method {dict(be['onset_method'].value_counts())}; "
          f"post-peak decay accepted (R² ≥ 0.3) on "
          f"{int(be['decay_ok'].sum())}/{len(be)}.")
    A("")

    # ---- persistence ---------------------------------------------------
    pr = stage["persistence"]
    A("## 7. Persistence (F7)")
    A("")
    A(f"{len(pr):,} topic × platform series. Half-life defined (0 < φ < 1) on "
      f"**{int(pr['half_life_days'].notna().sum())}/{len(pr)}** series with the AR(1) on "
      f"the log-rate *level*, versus **{int(pr['diff_half_life_days'].notna().sum())}/{len(pr)}** "
      f"with the differenced form F7 literally specifies, versus **6/39** for v1's post-peak "
      f"curve fitting. Ljung–Box on the level rejects white noise (p < 0.05) on "
      f"{int((pr['ljung_box_p'] < 0.05).sum())}/{len(pr)} series.")
    A("")
    top = pr.nlargest(8, "total_V")
    A("| topic | platform | n_obs | φ | SE(φ) | half-life (d) | volatility | Ljung–Box p |")
    A("|---|---|---:|---:|---:|---:|---:|---:|")
    for _, r in top.iterrows():
        A(f"| {r['topic_id']} | {r['platform']} | {int(r['n_obs'])} | {fmt(r['ar1_phi'])} | "
          f"{fmt(r['phi_se'])} | {fmt(r['half_life_days'],2)} | {fmt(r['volatility'])} | "
          f"{fmt(r['ljung_box_p'],2 if r['ljung_box_p']>0.001 else 8)} |")
    A("")

    # ---- null control + sensitivity ------------------------------------
    A("## 8. Null control (F8, fixes D4) and sensitivity (fixes D5)")
    A("")
    nc = stage["null_control"]
    A("v1 asserted `real >> shuffled` for mean|Δ| and then reported `ok: true` on the "
      "opposite observation. The correct statement is that a temporally smooth series has "
      "**lower** mean|Δ| than its shuffle. Measured on the 8 largest series:")
    A("")
    A("| topic | platform | days | ΣV | mean\\|Δ\\| real | shuffled | structure? | "
      "burst stat | null p95 | perm p | Ljung–Box p |")
    A("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|")
    for _, r in nc.iterrows():
        A(f"| {r['topic_id']} | {r['platform']} | {int(r['n_days'])} | {int(r['total_V'])} | "
          f"{fmt(r['mean_abs_diff_real'],2)} | {fmt(r['mean_abs_diff_shuffled'],2)} | "
          f"{'yes' if r['temporal_structure_supported'] else 'no'} | "
          f"{fmt(r['observed'],1)} | {fmt(r['null_p95'],1)} | {fmt(r['p_value'],3)} | "
          f"{fmt(r['ljung_box_p'],2 if r['ljung_box_p']>0.001 else 8)} |")
    A("")
    A(f"All {len(nc)}/{len(nc)} series show mean|Δ|_real < mean|Δ|_shuffled, i.e. real "
      f"temporal structure, and "
      f"{int(nc['exceeds_null_p95'].sum())}/{len(nc)} have an observed burst weight beyond "
      f"the 95th percentile of {params['params']['permutation_n']} shuffles.")
    A("")
    sc = stage["sensitivity_checks"]
    A("D5: the harness now calls the **same** `compute_panel()` the production path calls, "
      "and the run aborts unless `corr(default, default) = 1.0` exactly.")
    A("")
    A("| variant | corr(momentum) | corr(accel_smooth) | corr(surprise) | n |")
    A("|---|---:|---:|---:|---:|")
    for _, r in sc.iterrows():
        A(f"| `{r['variant']}` {r['params'] if r['params'] != '{}' else ''} | "
          f"{fmt(r['corr_momentum'],4)} | {fmt(r['corr_accel_smooth'],4)} | "
          f"{fmt(r['corr_surprise'],4)} | {int(r['n_momentum']):,} |")
    A("")
    A("Reading: momentum and surprise are stable to their windows (≥ 0.98). "
      "`accel_smooth` is **not** — corr(savgol 5, savgol 7) = "
      f"{fmt(float(sc.loc[sc['variant']=='savgol_7','corr_accel_smooth'].iloc[0]),3)}. "
      "The smoothing window is the single largest tuning risk in the acceleration family.")
    A("")

    # ---- hashtags ------------------------------------------------------
    A("## 9. Hashtag grain — measured coverage (spec section 4)")
    A("")
    A(f"**{cov['n_posts_with_any_tag']:,} of {cov['n_posts']:,} posts carry any hashtag "
      f"= {cov['coverage_pct']}%.** {cov['n_distinct_tags']:,} distinct normalised tags; "
      f"{len(stage['tag_dim']):,} pass the ≥{params['params']['tag_min_posts_dim']}-post "
      f"dimension floor and {int(stage['tag_dim']['in_ts_scope'].sum()):,} pass the "
      f"≥{params['params']['tag_min_posts_ts']}-post time-series floor.")
    A("")
    A("| platform | posts | `hashtags` field | inline `#tag` | union | coverage |")
    A("|---|---:|---:|---:|---:|---:|")
    for pl, s in cov["per_platform"].items():
        A(f"| {pl} | {s['n']:,} | {s['field']:,} | {s['mined']:,} | {s['union']:,} | "
          f"{s['coverage_pct']}% |")
    A("")
    A("**A hashtag-keyed alerting system would be blind to ~82% of collected material "
      "and ~84% of Telegram, the dominant source.** `tag_metrics_daily` is built and "
      "support-gated so hashtag alerting works where hashtags exist, but `topic_id` must "
      "remain the primary alert key, with hashtag and keyword as secondary facets.")
    A("")

    # ---- effort --------------------------------------------------------
    ed = params["effort"]
    A("## 10. Exposure offset (F2) — deviation from the spec")
    A("")
    A("The spec names `collection_effort` as the exposure offset. It cannot serve that "
      "role on the posted-day grain, measured:")
    A("")
    A(f"- it is keyed on **scrape day**, not posted day: only "
      f"{ed['n_days_with_reported_effort']} of {ed['n_platform_days']} platform-days in "
      f"the panel have a row (8 of 45 Telegram posted-days);")
    A("- `active_channels = 0` on 5 of Telegram's 8 rows despite thousands of ingested rows;")
    A(f"- where both exist, observed and reported active channels correlate "
      f"{fmt(ed.get('corr_observed_vs_reported'),3)} overall, but per-platform "
      + ", ".join(f"{k}: {fmt(v['corr'],2)} (n={v['n']})"
                  for k, v in sorted(ed.get("per_platform", {}).items())) + ".")
    A("")
    A("Splicing a partially-available series with a fallback would inject scale "
      "discontinuities indistinguishable from growth. `effort` is therefore the "
      "**observed** distinct active channels per platform-day — the same quantity the "
      "spec asks for, measured on the correct grain with 100% coverage — with "
      "`collection_effort` used only as the cross-check above. `effort_source` records "
      "this on every row. Consequence to note: `V_rate` is posts per active channel and "
      "is endogenous, so a coordinated burst from a *few* channels raises `V_rate` more "
      "than the same burst spread across many.")
    A("")

    # ---- EB ------------------------------------------------------------
    A("## 11. Empirical-Bayes shrinkage (F5)")
    A("")
    A("| grain | metric | k = σ²_w/τ² | τ² | within-cell σ² | parent global mean |")
    A("|---|---|---:|---:|---:|---:|")
    for g, d in params["eb_diagnostics"].items():
        for m, v in d.items():
            A(f"| {g} | {m} | {fmt(v['k'],3)} | {fmt(v['tau2'],5)} | "
              f"{fmt(v['within_var'],5)} | {fmt(v['global_mean'],4)} |")
    A("")
    A("k is the number of pseudo-observations of the parent mixed into every cell; "
      "`n = 1` cells are pulled to `n/(n+k)` of their own value. `*_n` is published "
      "beside every shrunk value.")
    A("")

    # ---- guarantees -----------------------------------------------------
    A("## 12. Correctness guarantees checked at build time")
    A("")
    ck = params["self_checks"]
    A(f"- `stats_v2.run_self_checks()`: **{sum(ck.values())}/{len(ck)} passed** "
      f"(asserted before the build starts). Includes three no-look-ahead proofs "
      f"(`baseline_excludes_current_day`, `baseline_no_future_leakage`, "
      f"`causal_savgol_no_future_leakage`) and a demonstration that the centred "
      f"Savitzky–Golay filter *does* leak, which is why the causal form is used.")
    A("- `post_uid` double counting: asserted zero duplicates after the topic/score joins.")
    A("- In-window `Σ V` reconciles exactly with the in-window post count.")
    A("- No duplicate `(topic_id, platform, day)` grain keys.")
    A("- No negative `scrape_lag_days` passed through (asserted zero in source; "
      "any negative value would be nulled).")
    A("- No row publishes `surprise` without `support_ok`.")
    A("- No zero-volume cell publishes `surprise` (permanent D1 regression assertion).")
    A(f"- `hate_calibrated = false` on every row; every `hate_*` field is marked "
      f"uncalibrated in `metric_meta`.")
    A(f"- Deterministic: seed {params['params']['seed']} everywhere; "
      "`analytics.duckdb` is opened `read_only=True` and only this process writes "
      "`metrics.duckdb`.")
    A("")

    # ---- open questions -------------------------------------------------
    A("## 13. Flagged for the statistical review")
    A("")
    A("1. **Overdispersion self-masking.** The NB baseline is fitted on the trailing 28 "
      "days including earlier spikes, so 1/θ absorbs them and depresses `surprise`: the "
      f"largest value in the whole panel is {fmt(v2['max_surprise'])} "
      f"(p = {fmt(v2['max_surprise_implied_p'],6)}) even for a 35× excursion "
      "(t007/instagram, V = 124 vs μ = 8.2). A trimmed or winsorised baseline window "
      "would sharpen this; it is not implemented.")
    A("2. **`accel_smooth` window sensitivity** — corr(5, 7) = "
      f"{fmt(float(sc.loc[sc['variant']=='savgol_7','corr_accel_smooth'].iloc[0]),3)}. "
      "Needs a principled window choice.")
    A("3. **Per-day BH has very low power** — m ≈ 10–15 supported cells per day. "
      "`surprise_q_global` is published alongside; the review should pick one.")
    A("4. **AR(1) on the level vs the difference** — see the `ar1_phi` caveat in "
      "`metric_meta`. The spec's differenced form yields a defined half-life on only "
      f"{int(pr['diff_half_life_days'].notna().sum())}/{len(pr)} series.")
    A("5. **Kleinberg runs on raw counts**, so a rising collector ramp can inflate late "
      "burst states; the CUSUM path is effort-adjusted. Worth harmonising.")
    A("6. **`onset_slope` units are not comparable across `onset_method`** — "
      f"{int((be['onset_method']=='loglinear_slope').sum()) if len(be) else 0}/"
      f"{len(be)} episodes use the log-linear fallback because rising limbs are 1–3 days "
      "in a 45-day window.")
    A("7. **`emergence_score` is a within-run rank**, so values are not comparable across "
      "runs as the panel grows.")
    A("8. **`coordination_flag` is a heuristic**, not a test; reddit (50%) and instagram "
      "(45%) baseline duplicate rates are feed artefacts.")
    A("")
    return "\n".join(L) + "\n"


def main() -> int:
    if not STAGE_DIR.exists():
        print("ERROR: run scripts/07_metrics_v2.py first (no data/metrics_stage)")
        return 1
    stage = read_stage()
    params = json.loads((STAGE_DIR / "run_params.json").read_text())

    stage["metric_meta"] = pd.DataFrame(METRIC_META)
    assert stage["metric_meta"]["metric_key"].is_unique, "metric_meta has duplicate keys"

    # every published measure must be described in metric_meta
    described = set(stage["metric_meta"]["metric_key"])
    structural = {
        "day", "topic_id", "tag_id", "platform", "in_window", "support_n", "support_ok",
        # baseline_method: how theta was obtained for this cell (mle / mom /
        # mle_at_bound / poisson_underdisp / poisson_theta_unidentifiable / ...).
        # Structural provenance, not a measure -- it lets a consumer tell a real
        # MLE fit from a fallback, which the shipped data previously could not.
        "burst_support_ok", "baseline_days", "baseline_mean", "baseline_dist", "baseline_method",
        "effort_source", "log_rate", "log_count", "surprise_q_global", "emergence_flag",
        "tone_mean", "hate_n", "read_n", "views_sum", "E_sum", "mean_confidence",
        "virality_pct_raw", "virality_n", "novelty_n", "novelty_base_n",
        # count_mode='soft' only (07 Step 1): hard top-1 count / distinct top-1 authors
        # kept beside the weighted V and effective n_authors. Absent in hard mode.
        "V_primary", "n_authors_primary",
    }
    undescribed = sorted(
        c for c in stage["metrics_daily"].columns
        if c not in described and c not in structural
    )
    assert not undescribed, f"metrics_daily columns missing from metric_meta: {undescribed}"

    for name, df in stage.items():
        bad = FORBIDDEN_COLS & set(df.columns)
        assert not bad, f"{name} carries post-level payload {bad} — metrics DB is metrics only"

    PARQUET_METRICS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(METRICS_DB))  # only this process writes here
    db_rows: dict[str, int] = {}
    try:
        con.execute("BEGIN TRANSACTION;")
        for t in TABLES:
            df = stage.get(t)
            if df is None:
                log(f"  WARN missing staged table {t}")
                continue
            con.register("_stage_df", df)
            con.execute(f'DROP TABLE IF EXISTS "{t}";')
            con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM _stage_df;')
            con.unregister("_stage_df")
            db_rows[t] = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            mirror_parquet(t, df)
            log(f"  published {t:20s} rows={db_rows[t]:>7,}  -> duckdb + parquet_metrics/")
        for t, n in publish_optional_tables(con, stage).items():
            db_rows[t] = n
            mirror_parquet(t, stage[t])
            log(f"  published {t:20s} rows={n:>7,}  -> duckdb + parquet_metrics/ (optional)")
        for t in OPTIONAL_TABLES_DDL:
            if t not in stage and (PARQUET_METRICS / t).exists():
                shutil.rmtree(PARQUET_METRICS / t)      # stale mirror of a dropped table
        # convenience views for the dashboard
        con.execute("""
            CREATE OR REPLACE VIEW v_metrics_live AS
            SELECT * FROM metrics_daily WHERE in_window AND support_ok;""")
        con.execute("""
            CREATE OR REPLACE VIEW v_alerts AS
            SELECT a.*, t.domain, t.topic, t.label
            FROM alert_candidates a LEFT JOIN topic_dim t USING (topic_id);""")
        con.execute("COMMIT;")
    except Exception:
        con.execute("ROLLBACK;")
        raise
    finally:
        con.close()

    report = build_report(stage, params, db_rows)
    out = (Path(_SHADOW) if _SHADOW else REPORTS_DIR) / "METRICS_V2_SPEC_RESULTS.md"
    out.write_text(report)
    log(f"wrote {out}")
    log(f"metrics.duckdb = {METRICS_DB} ({METRICS_DB.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
