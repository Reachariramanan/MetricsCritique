#!/usr/bin/env python3
"""Noise-gate evaluation harness: LEGACY method vs CANDIDATE method, same inputs.

The plan (``Noise gates`` in the workstream plan) says a step is adopted only if
its gates pass, with thresholds fixed BEFORE results exist and constants tuned on
the first half of the window and judged on the second. This module is the
machinery for that and nothing else: pure functions over arrays and DataFrames.
It opens no database, runs no pipeline stage and writes nothing, so it cannot
touch production Postgres or any DuckDB file. Callers (a review script, a
notebook) load the data, call these, and paste the verdict into
``docs/METRICS_V2_SPEC.md`` / ``collaboration/04_CURRENT_STATE.md``.

Gates covered here (numbers follow the plan):
  1  burst_null_ratio / compare_burst_null     real vs shuffled episodes
  2  chain_null_gate                           real links vs circular-shift null
  3  episode_stability / label_perturbation    Jaccard under label flips
  4  restatement_gate                          Spearman vs V (never Pearson)
  5  selftest_gate                             D5 corr(default, default) == 1.0
  6  sampling_plan / judge_precision           blind stratified judging, Wilson CI
  7  alert_churn / churn_gate                  every high-alert change explained

Not covered: chain ARI (needs the chain builder's cluster output), and running
the legacy/candidate pipelines themselves.

Design rule, repeated because this project has been burned by it: a gate that
lacks the data to decide returns INCONCLUSIVE, never PASS. ``verdict`` treats
INCONCLUSIVE exactly like FAIL for the adopt decision; the difference is only in
what the write-up says ("failed" vs "could not be tested").
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import stats_v2 as S  # noqa: E402

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
ADOPT, DO_NOT_ADOPT = "adopt", "do-not-adopt"

# --------------------------------------------------------------------------- #
# Thresholds. Fixed here, before any legacy-vs-candidate result exists. Changing
# one after seeing results is a spec change and must be recorded as such.
# --------------------------------------------------------------------------- #
SEED = S.RNG_SEED
Z95 = 1.959963984540054          # two-sided 95% normal quantile

# -- gate 1: burst permutation null -------------------------------------------
N_PERM = 200
# A 1-day "episode" is driven by the count marginal, which shuffling preserves,
# so a detector can fire on single spikes equally often in real and shuffled
# data. Measured on synthetic planted bursts: the all-episode ratio is ~0.3 for
# EVERY detector (a contiguous burst is 1 episode; shuffled it fragments into
# several), so it cannot rank detectors. Counting only multi-day episodes
# measures what a burst is: persistence that shuffling destroys.
MIN_EPISODE_DAYS = 2
# Pooled over series; below this the ratio is dominated by one or two series.
MIN_SERIES_BURST = 10
# real + mean-shuffled multi-day episodes, per method, pooled. A ratio of
# (2+1)/(0+1) is not evidence of anything.
MIN_BURST_EVENTS = 20
# "Must not fall" is the plan's rule: any point-estimate drop FAILs. PASS also
# needs the paired bootstrap over series to rule out a material drop, else the
# difference is noise and the gate is INCONCLUSIVE.
BURST_NONINFERIORITY = 0.10
N_BOOT = 1000

# -- gate 2: chain null --------------------------------------------------------
# The plan's rule is 'real links exceed the 95th percentile of shifted links'.
CHAIN_NULL_QUANTILE = 0.95
# With 19 shifts the 95th percentile is just the max of 19 draws; need a few
# hundred for the quantile to mean something. 100 is the floor.
MIN_CHAIN_SHIFTS = 100
MIN_CHAIN_LINKS = 5              # fewer real links than this: nothing to test

# -- gate 3: stability ---------------------------------------------------------
LABEL_FLIP_FRAC = 0.10           # plan: runner-up flip on 10% of ambiguous posts
N_FLIP_REPS = 5
MIN_STABILITY_KEYS = 10          # topic x platform keys with episodes
MIN_FLIPPED_POSTS = 10           # below this the perturbation is not a test

# -- gate 4: restatement -------------------------------------------------------
# Same numbers as scripts/audit_restatement.py: |rho| > 0.7 means "IS VOLUME".
RESTATEMENT_RHO_CEIL = 0.7
MIN_RESTATEMENT_N = 30           # audit_restatement.MIN_N
RHO_EPS = 1e-9                   # float slack only; the rule is strict |rho| <= legacy

# -- gate 5: D5 sensitivity self-test -------------------------------------------
SELFTEST_TOL = 1e-9              # 07 asserts abs(v - 1) < 1e-9; same tolerance

# -- gate 6: precision -----------------------------------------------------------
JUDGE_N_TARGET = 120             # plan: ~120 stratified items per method
MIN_JUDGED = 60                  # fewer judged items per method: INCONCLUSIVE
# Wilson half-width at p=0.8, n=60 is ~0.10, n=120 ~0.07: about the finest
# resolution that distinguishes a 0.80 classifier from a 0.65 one.
# Floor: this project has found labels at <50% precision shipped as findings.
# A method whose precision cannot be shown >= 0.50 (lower CI bound) is not usable.
PRECISION_FLOOR = 0.50
# Candidate may lose this much point precision vs legacy and still pass: at
# n=120 a +-0.05 difference is inside the sampling noise of either estimate.
PRECISION_NONINFERIORITY = 0.05

# -- gate 7: alert churn ---------------------------------------------------------
HIGH_SEVERITIES = ("high",)      # 07_metrics_v2 emits "high" (surprise_q<0.01) / "medium"
# Churn on a handful of alerts is anecdote, not a rate.
MIN_ALERTS_FOR_CHURN = 20

# Which gates a full step must clear. A step that legitimately skips one (e.g. a
# topic change has no chains) passes ``required=`` explicitly to ``verdict``;
# omitting a gate from the default is never silent.
ALL_GATES = ("burst_null", "chain_null", "stability", "restatement",
             "sensitivity", "precision", "alert_churn")

Detector = Callable[[np.ndarray, Optional[np.ndarray]], "S.BurstResult"]


# =========================================================================== #
# shared helpers
# =========================================================================== #
def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. Same formula as 14_cameo_event_coding.wilson(),
    re-implemented because that module has a numeric prefix and cannot be
    imported. Returns (0, 1) for n == 0: no data, no information."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _gate(status: str, reason: str, **detail) -> dict:
    return {"status": status, "reason": reason, **detail}


def _status_of(x) -> str:
    return x["status"] if isinstance(x, Mapping) else str(x)


# =========================================================================== #
# 1. burst permutation null
# =========================================================================== #
def legacy_burst_detector(**burst_kwargs) -> Detector:
    """Adapter: production ``S.detect_bursts`` as a (counts, exposure) detector.
    Exposure is ignored (the legacy path has no exposure input). Pass the same
    ``burst_*`` parameters 07 uses."""
    def det(counts, exposure=None):
        return S.detect_bursts(counts, **burst_kwargs)
    return det


def _episode_count(det: Detector, counts: np.ndarray, expo: Optional[np.ndarray],
                   min_days: int) -> int:
    r = det(counts, expo)
    eps = S.extract_episodes(r.state, counts, r.intensity)
    return sum(1 for e in eps if e["n_days"] >= min_days)


def _unpack_series(item) -> tuple[np.ndarray, Optional[np.ndarray]]:
    if isinstance(item, tuple) and len(item) == 2:
        c, e = item
    else:
        c, e = item, None
    c = np.asarray(c, dtype=float)
    c = np.where(np.isfinite(c), c, 0.0)
    e = None if e is None else np.asarray(e, dtype=float)
    return c, e


def burst_null_ratio(series_list: Sequence, detector: Detector, *,
                     n_perm: int = N_PERM, seed: int = SEED,
                     min_days: int = MIN_EPISODE_DAYS) -> dict:
    """Real vs shuffled episode counts for one detector.

    ``series_list`` items are ``counts`` or ``(counts, exposure)``. Shuffles are
    day permutations applied to counts and exposure jointly (the rate/effort
    relation survives, temporal order does not). The permutation stream for
    series i depends only on (seed, i), so a legacy and a candidate detector see
    byte-identical shuffles; their difference is not Monte-Carlo noise.

    ``ratio`` = (real + 1) / (mean shuffled + 1), pooled over series. The +1 keeps
    a 0-in-shuffle series from producing inf and shrinks low-count ratios toward
    1, which is the conservative direction. Per-series numbers are returned so a
    caller (or ``compare_burst_null``) can bootstrap over series.
    """
    rows = []
    for i, item in enumerate(series_list):
        x, ex = _unpack_series(item)
        rng = np.random.default_rng([seed, i])
        real = _episode_count(detector, x, ex, min_days)
        shuf = np.empty(n_perm)
        for b in range(n_perm):
            p = rng.permutation(x.size)
            shuf[b] = _episode_count(detector, x[p], None if ex is None else ex[p], min_days)
        rows.append({"series": i, "n_days": int(x.size), "real": int(real),
                     "shuffled_mean": float(shuf.mean())})
    per = pd.DataFrame(rows)
    real_t = float(per["real"].sum()) if len(per) else 0.0
    shuf_t = float(per["shuffled_mean"].sum()) if len(per) else 0.0
    return {
        "per_series": per,
        "n_series": int(len(per)),
        "real_episodes": real_t,
        "shuffled_episodes": shuf_t,
        "ratio": (real_t + 1.0) / (shuf_t + 1.0),
        "min_days": min_days,
        "n_perm": n_perm,
    }


def _pooled_ratio(real: np.ndarray, shuf: np.ndarray) -> float:
    return (real.sum() + 1.0) / (shuf.sum() + 1.0)


def compare_burst_null(series_list: Sequence, legacy: Detector, candidate: Detector,
                       *, n_perm: int = N_PERM, seed: int = SEED,
                       min_days: int = MIN_EPISODE_DAYS, n_boot: int = N_BOOT) -> dict:
    """Gate 1: the candidate's real/shuffled ratio must not fall vs legacy."""
    L = burst_null_ratio(series_list, legacy, n_perm=n_perm, seed=seed, min_days=min_days)
    C = burst_null_ratio(series_list, candidate, n_perm=n_perm, seed=seed, min_days=min_days)
    diff = C["ratio"] - L["ratio"]
    n = L["n_series"]
    out = dict(legacy_ratio=L["ratio"], candidate_ratio=C["ratio"], diff=diff,
               legacy=L, candidate=C, n_series=n)
    if n < MIN_SERIES_BURST:
        return _gate(INCONCLUSIVE, f"{n} series < {MIN_SERIES_BURST}", **out)
    ev_l = L["real_episodes"] + L["shuffled_episodes"]
    ev_c = C["real_episodes"] + C["shuffled_episodes"]
    if min(ev_l, ev_c) < MIN_BURST_EVENTS:
        return _gate(INCONCLUSIVE,
                     f"pooled episodes legacy={ev_l:.1f} candidate={ev_c:.1f} < {MIN_BURST_EVENTS}",
                     **out)
    if diff < 0:
        return _gate(FAIL, f"ratio fell: {L['ratio']:.3f} -> {C['ratio']:.3f}", **out)
    # paired bootstrap over series (same series resampled for both methods)
    rng = np.random.default_rng([seed, 99991])
    lr, ls = L["per_series"]["real"].to_numpy(float), L["per_series"]["shuffled_mean"].to_numpy(float)
    cr, cs = C["per_series"]["real"].to_numpy(float), C["per_series"]["shuffled_mean"].to_numpy(float)
    d = np.empty(n_boot)
    for b in range(n_boot):
        ix = rng.integers(0, n, n)
        d[b] = _pooled_ratio(cr[ix], cs[ix]) - _pooled_ratio(lr[ix], ls[ix])
    lo = float(np.quantile(d, 0.05))
    out["diff_boot_p05"] = lo
    if lo < -BURST_NONINFERIORITY:
        return _gate(INCONCLUSIVE,
                     f"ratio did not fall ({diff:+.3f}) but bootstrap p05 {lo:+.3f} "
                     f"cannot exclude a drop > {BURST_NONINFERIORITY}", **out)
    return _gate(PASS, f"ratio {L['ratio']:.3f} -> {C['ratio']:.3f} (boot p05 {lo:+.3f})", **out)


# =========================================================================== #
# 2. chain null
# =========================================================================== #
def chain_null_gate(real_links: float, shifted_links: Sequence[float]) -> dict:
    """Real chain links must exceed the 95th percentile of links recounted after
    circularly shifting each series' episode times. The shifting itself needs
    the chain builder, so this takes its output."""
    sh = np.asarray(shifted_links, dtype=float)
    sh = sh[np.isfinite(sh)]
    if sh.size < MIN_CHAIN_SHIFTS:
        return _gate(INCONCLUSIVE, f"{sh.size} shifts < {MIN_CHAIN_SHIFTS}")
    if not np.isfinite(real_links) or real_links < MIN_CHAIN_LINKS:
        return _gate(INCONCLUSIVE, f"{real_links} real links < {MIN_CHAIN_LINKS}")
    q = float(np.quantile(sh, CHAIN_NULL_QUANTILE))
    ok = real_links > q
    return _gate(PASS if ok else FAIL,
                 f"real {real_links:g} {'>' if ok else '<='} null p{int(CHAIN_NULL_QUANTILE*100)} {q:g}",
                 real_links=float(real_links), null_quantile=q, n_shifts=int(sh.size))


# =========================================================================== #
# 3. stability
# =========================================================================== #
def _expand(start, end) -> set:
    if isinstance(start, (int, np.integer)) and isinstance(end, (int, np.integer)):
        return set(range(int(start), int(end) + 1))
    s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    return set(pd.date_range(s, e, freq="D"))


def _day_sets(eps: pd.DataFrame) -> dict:
    out: dict = {}
    if eps is None or not len(eps):
        return out
    for t, p, s, e in zip(eps["topic_id"], eps["platform"], eps["start_day"], eps["end_day"]):
        out.setdefault((t, p), set()).update(_expand(s, e))
    return out


def episode_stability(episodes_a: pd.DataFrame, episodes_b: pd.DataFrame) -> dict:
    """Jaccard of episode day-sets, matched by (topic_id, platform).

    Inputs have columns topic_id, platform, start_day, end_day (inclusive; ints
    or dates). A key with episodes in only one side scores 0 -- an episode that
    appears or vanishes under perturbation is instability, and dropping it would
    flatter the method. Returns the unweighted mean over the union of keys.
    """
    A, B = _day_sets(episodes_a), _day_sets(episodes_b)
    rows = []
    for k in sorted(set(A) | set(B), key=str):
        a, b = A.get(k, set()), B.get(k, set())
        u = len(a | b)
        rows.append({"topic_id": k[0], "platform": k[1], "n_a": len(a), "n_b": len(b),
                     "matched": bool(a and b), "jaccard": (len(a & b) / u) if u else np.nan})
    per = pd.DataFrame(rows, columns=["topic_id", "platform", "n_a", "n_b", "matched", "jaccard"])
    ok = per["jaccard"].notna()
    return {
        "per_key": per,
        "n_keys": int(ok.sum()),
        "n_matched": int(per["matched"].sum()),
        "mean_jaccard": float(per.loc[ok, "jaccard"].mean()) if ok.any() else np.nan,
    }


def label_perturbation(posts: pd.DataFrame,
                       rebuild_episodes: Callable[[pd.DataFrame], pd.DataFrame],
                       *, frac: float = LABEL_FLIP_FRAC, n_rep: int = N_FLIP_REPS,
                       seed: int = SEED, topic_col: str = "topic_id",
                       runner_up_col: str = "runner_up_topic_id",
                       ambiguous_col: str = "ambiguous") -> dict:
    """Flip ``frac`` of the ambiguous posts to their runner-up topic and measure
    how much the episodes move.

    ``rebuild_episodes(posts)`` must recompute episodes from post membership with
    the method under test (legacy or candidate) and return the frame
    ``episode_stability`` takes. Eligible posts: ``ambiguous_col`` true, runner-up
    present and different from the assigned topic. Flip sets depend only on
    (seed, rep) and the post order, so legacy and candidate see the same flips.
    """
    base = rebuild_episodes(posts)
    elig = np.flatnonzero(
        posts[ambiguous_col].fillna(False).to_numpy(bool)
        & posts[runner_up_col].notna().to_numpy()
        & (posts[runner_up_col].to_numpy() != posts[topic_col].to_numpy()))
    n_flip = int(round(frac * elig.size))
    n_flip = max(1, n_flip) if elig.size else 0
    reps, jac = [], []
    for r in range(n_rep):
        rng = np.random.default_rng([seed, r])
        pick = rng.choice(elig, size=n_flip, replace=False) if n_flip else np.array([], int)
        q = posts.copy()
        col = q.columns.get_loc(topic_col)
        q.iloc[pick, col] = posts[runner_up_col].to_numpy()[pick]
        st = episode_stability(base, rebuild_episodes(q))
        reps.append(st)
        jac.append(st["mean_jaccard"])
    jac = np.asarray(jac, dtype=float)
    return {
        "mean_jaccard": float(np.nanmean(jac)) if np.isfinite(jac).any() else np.nan,
        "per_rep": jac.tolist(),
        "n_ambiguous": int(elig.size),
        "n_flipped": n_flip,
        "n_keys": int(min((s["n_keys"] for s in reps), default=0)),
        "n_rep": n_rep,
        "frac": frac,
    }


def stability_gate(legacy: Mapping, candidate: Mapping) -> dict:
    """Gate 3: candidate episode Jaccard >= legacy, on the same perturbation.
    Takes two ``label_perturbation`` (or ``episode_stability``) results."""
    lj, cj = legacy["mean_jaccard"], candidate["mean_jaccard"]
    info = dict(legacy_jaccard=lj, candidate_jaccard=cj)
    for name, r in (("legacy", legacy), ("candidate", candidate)):
        if r.get("n_keys", 0) < MIN_STABILITY_KEYS:
            return _gate(INCONCLUSIVE, f"{name}: {r.get('n_keys', 0)} keys < {MIN_STABILITY_KEYS}", **info)
        if "n_flipped" in r and r["n_flipped"] < MIN_FLIPPED_POSTS:
            return _gate(INCONCLUSIVE, f"{name}: {r['n_flipped']} flipped posts < {MIN_FLIPPED_POSTS}", **info)
    if not (np.isfinite(lj) and np.isfinite(cj)):
        return _gate(INCONCLUSIVE, "non-finite Jaccard", **info)
    ok = cj >= lj
    return _gate(PASS if ok else FAIL, f"Jaccard {lj:.3f} -> {cj:.3f}", **info)


# =========================================================================== #
# 4. restatement
# =========================================================================== #
def restatement_gate(metric_df: pd.DataFrame, v_col: str, cols: Sequence[str],
                     legacy_rho: Optional[Mapping[str, float]] = None) -> dict:
    """Spearman of each candidate metric against volume ``v_col``.

    Spearman, not Pearson: daily counts are heavy-tailed and Pearson mis-flags
    (docs/metrics-critique.md; audit_restatement.py measured burst_intensity at
    Pearson +0.808 but Spearman +0.448). Pass rule per column, from the plan:
    |rho| <= |legacy rho| AND |rho| < 0.7. A column with no legacy value is held
    to the absolute rule alone and says so. Pass ``metric_df`` already filtered
    to supported rows (``support_ok``); NaN pairs are dropped here.
    Gate status: FAIL if any column FAILs, INCONCLUSIVE if any is untestable and
    none failed, PASS only if every column PASSes.
    """
    legacy_rho = legacy_rho or {}
    per = {}
    for c in cols:
        if c not in metric_df.columns or v_col not in metric_df.columns:
            per[c] = {"status": INCONCLUSIVE, "reason": "column missing", "rho": np.nan, "n": 0}
            continue
        s = metric_df[[v_col, c]].dropna()
        n = len(s)
        if n < MIN_RESTATEMENT_N or s[c].nunique() < 2 or s[v_col].nunique() < 2:
            per[c] = {"status": INCONCLUSIVE, "n": n, "rho": np.nan,
                      "reason": f"n={n} < {MIN_RESTATEMENT_N} or constant column"}
            continue
        rho = float(spearmanr(s[v_col], s[c]).statistic)
        leg = legacy_rho.get(c)
        has_leg = leg is not None and np.isfinite(leg)
        if abs(rho) >= RESTATEMENT_RHO_CEIL:
            st, why = FAIL, f"|rho| {abs(rho):.3f} >= {RESTATEMENT_RHO_CEIL}"
        elif has_leg and abs(rho) > abs(leg) + RHO_EPS:
            st, why = FAIL, f"|rho| {abs(rho):.3f} > legacy {abs(leg):.3f}"
        else:
            st, why = PASS, (f"|rho| {abs(rho):.3f} <= legacy {abs(leg):.3f}" if has_leg
                             else f"|rho| {abs(rho):.3f}; no legacy value, absolute rule only")
        per[c] = {"status": st, "reason": why, "rho": rho, "n": n,
                  "legacy_rho": float(leg) if has_leg else np.nan,
                  "rule": "legacy+absolute" if has_leg else "absolute_only"}
    sts = [p["status"] for p in per.values()]
    if not sts:
        return _gate(INCONCLUSIVE, "no columns to test", per_col=per)
    overall = FAIL if FAIL in sts else (INCONCLUSIVE if INCONCLUSIVE in sts else PASS)
    bad = [c for c, p in per.items() if p["status"] != PASS]
    return _gate(overall, "all columns pass" if overall == PASS else f"not passing: {bad}", per_col=per)


# =========================================================================== #
# 5. D5 sensitivity self-test
# =========================================================================== #
def selftest_gate(corrs: Mapping[str, float]) -> dict:
    """The D5 self-test (corr(default, default) per reference column, computed
    by 07's ``_variant("SELFTEST_default_vs_default")``) must be exactly 1.0 for
    the new ``hmm_*`` / ``soft_*`` variants to be trusted. Missing/NaN is
    INCONCLUSIVE, not PASS."""
    if not corrs:
        return _gate(INCONCLUSIVE, "no self-test values supplied")
    bad = {k: v for k, v in corrs.items()
           if v is None or not np.isfinite(v) or abs(v - 1.0) >= SELFTEST_TOL}
    nan = {k for k, v in bad.items() if v is None or not np.isfinite(v)}
    if bad and len(nan) == len(bad):
        return _gate(INCONCLUSIVE, f"non-finite self-test: {sorted(nan)}")
    if bad:
        return _gate(FAIL, f"self-test != 1.0: {bad}")
    return _gate(PASS, "corr(default, default) == 1.0", n_cols=len(corrs))


# =========================================================================== #
# 6. blind judging
# =========================================================================== #
def _allocate(sizes: pd.Series, n: int) -> pd.Series:
    """Proportional allocation with >=1 per non-empty stratum when affordable,
    capped at stratum size, largest-remainder rounding. Proportional alone gives
    small strata zero items, and small strata are where classifiers fail."""
    sizes = sizes[sizes > 0]
    n = min(n, int(sizes.sum()))
    if n <= 0 or not len(sizes):
        return sizes * 0
    base = pd.Series(1 if n >= len(sizes) else 0, index=sizes.index).clip(upper=sizes)
    left = n - int(base.sum())
    for _ in range(len(sizes) + n):
        if left <= 0:
            break
        room = sizes - base
        w = (room * (sizes / sizes.sum())).where(room > 0, 0.0)
        if w.sum() <= 0:
            break
        share = w / w.sum() * left
        add = np.floor(share).astype(int).clip(upper=room)
        if add.sum() == 0:  # hand out the remainder one by one, largest fraction first
            for k in (share - np.floor(share)).sort_values(ascending=False).index:
                if left <= 0:
                    break
                if room[k] > 0:
                    base[k] += 1
                    left -= 1
            continue
        base += add
        left -= int(add.sum())
    return base


def sampling_plan(df: pd.DataFrame, n: int, strata_cols: Sequence[str], seed: int = SEED,
                  *, method_col: str = "method", item_col: Optional[str] = None,
                  hide_cols: Iterable[str] = ()) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stratified blind-judging sample: ``n`` items PER METHOD (plan: ~120 each).

    ``df`` holds the candidate items of every method, one row per (item, method),
    with ``method_col`` naming the source. Items are drawn within each method,
    stratified on ``strata_cols``, then the union is shuffled.

    Returns ``(sheet, key)``:
      sheet: opaque ``item_id`` (assigned AFTER shuffling, so order and ids carry
             no method information), every original column EXCEPT ``method_col``
             and ``hide_cols``, and an empty ``label`` column to fill in.
      key:   long frame (item_id, method) for un-blinding after judging.
    With ``item_col``, an item drawn by both methods appears once in the sheet
    (judged once, so the two methods cannot be judged inconsistently) and twice
    in the key. Deterministic in (df order, n, strata_cols, seed).
    """
    if method_col not in df.columns:
        raise ValueError(f"missing method column {method_col!r}")
    strata_cols = list(strata_cols)
    picks = []
    for m in sorted(df[method_col].unique(), key=str):
        sub = df[df[method_col] == m]
        grp = sub.groupby(strata_cols, sort=True, dropna=False) if strata_cols else [((), sub)]
        groups = {k: g for k, g in grp}
        sizes = pd.Series({k: len(g) for k, g in groups.items()})
        alloc = _allocate(sizes, n)
        for k, g in groups.items():
            take = int(alloc.get(k, 0))
            if take <= 0:
                continue
            rng = np.random.default_rng([seed, _stable(m), _stable(k)])
            ix = rng.choice(len(g), size=take, replace=False)
            picks.append(g.iloc[np.sort(ix)])
    if not picks:
        empty = df.drop(columns=[method_col]).iloc[0:0].assign(item_id=[], label=[])
        return empty, pd.DataFrame(columns=["item_id", "method"])
    chosen = pd.concat(picks)
    if item_col:
        sheet_rows = chosen.drop_duplicates(item_col).copy()
    else:
        sheet_rows = chosen.copy()
    order = np.random.default_rng([seed, 7]).permutation(len(sheet_rows))
    sheet_rows = sheet_rows.iloc[order].reset_index(drop=True)
    sheet_rows["item_id"] = [f"J{i + 1:04d}" for i in range(len(sheet_rows))]
    if item_col:
        key = (chosen[[item_col, method_col]].merge(sheet_rows[[item_col, "item_id"]], on=item_col)
               [["item_id", method_col]].rename(columns={method_col: "method"}))
    else:
        # no item identity: row i of `chosen` is row order[i] of the shuffled sheet
        inv = {int(src): dst for dst, src in enumerate(order)}
        ids = [f"J{inv[i] + 1:04d}" for i in range(len(chosen))]
        key = pd.DataFrame({"item_id": ids, "method": chosen[method_col].to_numpy()})
    key = key.sort_values("item_id").reset_index(drop=True)
    drop = [method_col, *hide_cols]
    sheet = sheet_rows.drop(columns=[c for c in drop if c in sheet_rows.columns])
    sheet["label"] = np.nan
    cols = ["item_id"] + [c for c in sheet.columns if c != "item_id"]
    return sheet[cols], key


def _stable(x) -> int:
    """Process-independent small int for seeding (Python's str hash is salted)."""
    import zlib
    return zlib.crc32(repr(x).encode()) & 0x7FFFFFFF


def judge_precision(labels: Iterable) -> dict:
    """Precision with a Wilson 95% CI from hand labels (True/1 = correct,
    False/0 = wrong, NaN/None = not judged: excluded and counted)."""
    arr = pd.Series(list(labels), dtype="object")
    judged = arr.dropna()
    k = int(sum(bool(v) for v in judged))
    n = int(len(judged))
    lo, hi = wilson_ci(k, n)
    return {"n": n, "k": k, "precision": (k / n) if n else np.nan,
            "ci_lo": lo, "ci_hi": hi, "n_unjudged": int(len(arr) - n)}


def precision_by_method(sheet_labelled: pd.DataFrame, key: pd.DataFrame,
                        label_col: str = "label") -> dict:
    """Un-blind: join judged labels to the key and score each method."""
    j = key.merge(sheet_labelled[["item_id", label_col]], on="item_id", how="left")
    return {m: judge_precision(g[label_col]) for m, g in j.groupby("method")}


def precision_gate(legacy: Mapping, candidate: Mapping) -> dict:
    """Gate 6 on two ``judge_precision`` results.

    PASS needs: both methods have >= MIN_JUDGED judged items; candidate Wilson
    lower bound >= PRECISION_FLOOR (we can show it is not a coin flip); candidate
    point precision >= legacy - PRECISION_NONINFERIORITY.
    FAIL: candidate's upper bound is below the floor, or it is worse than legacy
    by more than the margin. Otherwise (CI straddles the floor) INCONCLUSIVE.
    """
    info = dict(legacy=legacy, candidate=candidate)
    for name, r in (("legacy", legacy), ("candidate", candidate)):
        if r["n"] < MIN_JUDGED:
            return _gate(INCONCLUSIVE, f"{name}: {r['n']} judged < {MIN_JUDGED}", **info)
    p_l, p_c = legacy["precision"], candidate["precision"]
    if candidate["ci_hi"] < PRECISION_FLOOR:
        return _gate(FAIL, f"candidate precision {p_c:.3f} (CI hi {candidate['ci_hi']:.3f}) < floor", **info)
    if p_c < p_l - PRECISION_NONINFERIORITY:
        return _gate(FAIL, f"candidate {p_c:.3f} < legacy {p_l:.3f} - {PRECISION_NONINFERIORITY}", **info)
    if candidate["ci_lo"] < PRECISION_FLOOR:
        return _gate(INCONCLUSIVE,
                     f"candidate CI lower {candidate['ci_lo']:.3f} cannot exclude precision < {PRECISION_FLOOR}",
                     **info)
    return _gate(PASS, f"candidate {p_c:.3f} [{candidate['ci_lo']:.3f},{candidate['ci_hi']:.3f}] "
                       f"vs legacy {p_l:.3f}", **info)


# =========================================================================== #
# 7. alert churn
# =========================================================================== #
ALERT_KEY = ["topic_id", "platform", "day"]


def alert_churn(legacy_alerts: pd.DataFrame, new_alerts: pd.DataFrame) -> dict:
    """Added / removed / kept alerts keyed on topic_id x platform x day.

    Keyed on topic_id, not hashtag (docs/ALERTING_KEYING.md). If a side has
    several rows per key (one per metric), the HIGHEST severity represents the
    key. ``high_changes`` lists what the plan requires explaining: every added or
    removed alert whose severity is in HIGH_SEVERITIES, plus kept alerts that
    crossed the high boundary.
    """
    def collapse(df):
        d = df[ALERT_KEY + ["severity"]].copy()
        d["_hi"] = d["severity"].isin(HIGH_SEVERITIES)
        d = d.sort_values("_hi", ascending=False).drop_duplicates(ALERT_KEY)
        return d.drop(columns="_hi").set_index(ALERT_KEY)["severity"]
    L, N = collapse(legacy_alerts), collapse(new_alerts)
    both = L.index.intersection(N.index)
    added, removed = N.index.difference(L.index), L.index.difference(N.index)
    kept = pd.DataFrame({"legacy_severity": L.loc[both], "new_severity": N.loc[both]}).reset_index()
    changed = kept[kept["legacy_severity"] != kept["new_severity"]]
    hi = set(HIGH_SEVERITIES)
    rows = []
    for k in added:
        if N[k] in hi:
            rows.append({**dict(zip(ALERT_KEY, k)), "change": "added", "legacy_severity": None, "new_severity": N[k]})
    for k in removed:
        if L[k] in hi:
            rows.append({**dict(zip(ALERT_KEY, k)), "change": "removed", "legacy_severity": L[k], "new_severity": None})
    for _, r in changed.iterrows():
        if (r["legacy_severity"] in hi) != (r["new_severity"] in hi):
            rows.append({**{c: r[c] for c in ALERT_KEY}, "change": "severity_changed",
                         "legacy_severity": r["legacy_severity"], "new_severity": r["new_severity"]})
    cols = ALERT_KEY + ["change", "legacy_severity", "new_severity"]
    return {
        "n_legacy": int(len(L)), "n_new": int(len(N)),
        "n_added": int(len(added)), "n_removed": int(len(removed)),
        "n_kept": int(len(both)), "n_severity_changed": int(len(changed)),
        "added": N.loc[added].reset_index(), "removed": L.loc[removed].reset_index(),
        "kept": kept,
        "high_changes": pd.DataFrame(rows, columns=cols),
    }


def churn_gate(churn: Mapping, explanations: Optional[Mapping[tuple, str]] = None) -> dict:
    """Gate 7: every high-alert change needs a written explanation, keyed by
    (topic_id, platform, day). Unexplained churn counts as noise -> FAIL.
    Too few alerts to speak of -> INCONCLUSIVE (no churn is not stability when
    there is nothing to churn)."""
    explanations = explanations or {}
    if churn["n_legacy"] + churn["n_new"] < MIN_ALERTS_FOR_CHURN:
        return _gate(INCONCLUSIVE, f"{churn['n_legacy']}+{churn['n_new']} alerts < {MIN_ALERTS_FOR_CHURN}")
    hc = churn["high_changes"]
    miss = [tuple(r[c] for c in ALERT_KEY) for _, r in hc.iterrows()
            if not str(explanations.get(tuple(r[c] for c in ALERT_KEY), "")).strip()]
    if miss:
        return _gate(FAIL, f"{len(miss)}/{len(hc)} high-alert changes unexplained",
                     unexplained=miss, n_high_changes=int(len(hc)))
    return _gate(PASS, f"{len(hc)} high-alert changes, all explained", n_high_changes=int(len(hc)))


# =========================================================================== #
# 5b. time split
# =========================================================================== #
def half_split(days) -> tuple[np.ndarray, np.ndarray]:
    """Boolean masks (first_half, second_half) over ``days``, split at the median
    DISTINCT day so a day never straddles the split. Tune constants on the first,
    report on the second. Raises on null days (a null timestamp would silently
    fall out of both halves -- a known failure mode here) and on < 2 distinct days."""
    s = pd.Series(np.asarray(days)).reset_index(drop=True)
    if s.isna().any():
        raise ValueError(f"{int(s.isna().sum())} null day(s): refusing to drop them silently")
    u = np.sort(s.unique())
    if len(u) < 2:
        raise ValueError("need >= 2 distinct days to split")
    cut = u[len(u) // 2]
    second = (s >= cut).to_numpy()
    return ~second, second


# =========================================================================== #
# 7b. verdict
# =========================================================================== #
def verdict(results: Mapping[str, object], required: Sequence[str] = ALL_GATES) -> dict:
    """Combine gate results into an adopt / do-not-adopt decision.

    ``results``: gate name -> gate dict (with ``status``) or a bare status string.
    A required gate that is absent is INCONCLUSIVE. Adopt iff every required gate
    PASSes: FAIL and INCONCLUSIVE both block. ``blocking`` says which and why, so
    the write-up can distinguish "failed" from "could not be tested". Gates in
    ``results`` but not in ``required`` are reported, never counted.
    """
    gates = {}
    for g in required:
        gates[g] = _status_of(results[g]) if g in results else INCONCLUSIVE
    extra = {g: _status_of(v) for g, v in results.items() if g not in required}
    blocking = {g: s for g, s in gates.items() if s != PASS}
    reasons = {g: (results[g].get("reason") if g in results and isinstance(results[g], Mapping)
                   else "gate not run") for g in blocking}
    return {
        "gates": gates,
        "informational": extra,
        "overall": ADOPT if not blocking else DO_NOT_ADOPT,
        "blocking": blocking,
        "blocking_reasons": reasons,
        "n_pass": sum(s == PASS for s in gates.values()),
        "n_required": len(gates),
    }
