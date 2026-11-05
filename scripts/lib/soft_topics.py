"""Soft top-2 topic membership (plan "Step 1": soft topic assignment).

Pure numpy/pandas, no DB and no `.env` import, so it is unit-testable offline
(scripts/tests/test_soft_topics.py).

Input is the effective assignment table written by `scripts/16_assign_new_posts_topics.py`
(`TOPIC_EXT_TABLE`, columns post_uid, platform, topic_id, parent_topic_id, sim_1,
topic_id_2, parent_topic_id_2, sim_2, margin, assign_method, source), where
`margin = sim_1 - sim_2` is the cosine margin between the best and runner-up centroid.

Rules
-----
* A post is AMBIGUOUS iff ``margin < M_HI`` AND both topics exist AND their parents DIFFER.
  Two topics sharing a parent are near-duplicates: the post stays hard on topic_id (no split).
  A topic with no parent is treated as its own parent, so two distinct parentless topics
  count as different parents.
* Weights: ``w1 = sigmoid(margin / TAU)``, ``w2 = 1 - w1``. Since margin >= 0, w1 is in
  [0.5, 1): margin 0 gives 0.5 / 0.5 and a large margin gives ~1. ``w2`` is computed as
  ``1 - w1`` (exact in IEEE for w1 in [0.5, 1]), so the weights of one post sum to exactly 1.
* Unambiguous posts: ``w = 1`` on `topic_id`. Posts with no topic (`below_threshold`,
  topic_id NULL) keep topic ``'unassigned'`` with ``w = 1``: no topic is invented for them.
* The posts' own `topic_id` must equal the extension table's `topic_id`; if it does not
  (e.g. the metrics run is on a different topic basis) the post stays hard, because the
  margin refers to the extension table's top-1.

Tuning discipline (plan "Tuning leakage")
-----------------------------------------
`choose_constants` needs an explicit boolean ``tune_mask``. Pass the FIRST HALF of the
analysis window (see `split_halves`) and evaluate everything downstream (episodes, churn,
precision, noise gates) on the SECOND half. There is deliberately no default mask, so the
constants cannot silently be fitted on the full data.

Wiring into scripts/07_metrics_v2.py (to be applied by the lead; 07 is NOT edited here)
---------------------------------------------------------------------------------------
Where: after ``posts = enrich_posts(ts_posts, P)`` and before grain 1/2, only when
``PARAMS["count_mode"] == "soft"``::

    from lib import soft_topics as ST
    # ext_df = SELECT * FROM geo.<TOPIC_EXT_TABLE>  (read-only, already attached)
    posts_soft = ST.expand_posts(posts, ext_df, tau=P["soft_tau"], m_hi=P["soft_m_hi"])
    cells_tp = aggregate_cells(posts_soft, ["topic_id", "platform", "day"], weight_col="w")
    cells_t  = aggregate_cells(posts_soft, ["topic_id", "day"],            weight_col="w")
    # tag grain (grain 3) and `effort` stay on the hard `posts` (tags/effort are not topics).

Patch to `aggregate_cells` (keep the old body as the `weight_col is None` branch so the
hard path stays byte-identical; `V`, `n_authors`... only change when a weight column is given)::

    def aggregate_cells(posts, keys, weight_col=None):
        if weight_col is None:
            return _aggregate_cells_hard(posts, keys)       # = today's body, unchanged
        W = "_w"
        p = posts.assign(_w=posts[weight_col].astype(float))
        # pre-multiply so plain groupby sums become weighted sums
        for c in ("tone_v", "hate_v", "read_v", "virality_pct_post",
                  "views", "E_sum", "reach_audience_norm_post", "confidence"):
            p["_wx_" + c] = p[c] * p[W]                      # NaN stays NaN
            p["_wn_" + c] = p[W].where(p[c].notna(), 0.0)    # weight of NON-NULL rows
        p["_wx_tone_ss"] = p["tone_v"] ** 2 * p[W]
        p["_wx_hate_ss"] = p["hate_v"] ** 2 * p[W]
        p["_wx_vir_ss"]  = p["virality_pct_post"] ** 2 * p[W]
        p["_whit"]  = p[W].where(p["hate_v"] >= 0.5, 0.0).where(p["hate_v"].notna(), 0.0)
        p["_wread_ok"] = p["read_ok"].astype(float) * p[W]
        g = p.groupby(keys, dropna=False)
        base = g.agg(
            V=(W, "sum"),                                    # V_soft = sum of w
            V_primary=("topic_rank", lambda s: float((s == 1).sum())),   # hard count
            tone_sum=("_wx_tone_v", "sum"),  tone_n=("_wn_tone_v", "sum"),
            hate_sum=("_wx_hate_v", "sum"),  hate_n=("_wn_hate_v", "sum"),
            hate_hits=("_whit", "sum"),
            read_sum=("_wx_read_v", "sum"),  read_n=("_wn_read_v", "sum"),
            vir_sum=("_wx_virality_pct_post", "sum"),
            vir_n=("_wn_virality_pct_post", "sum"),
            views_sum=("_wx_views", "sum"),  E_sum=("_wx_E_sum", "sum"),
            reach_norm_sum=("_wx_reach_audience_norm_post", "sum"),
            reach_norm_n=("_wn_reach_audience_norm_post", "sum"),
            conf_sum=("_wx_confidence", "sum"), conf_n=("_wn_confidence", "sum"),
            read_cov_num=("_wread_ok", "sum"),
            tone_ss=("_wx_tone_ss", "sum"), hate_ss=("_wx_hate_ss", "sum"),
            vir_ss=("_wx_vir_ss", "sum"),
        ).reset_index()
        base["read_cov"] = base["read_cov_num"] / base["V"].replace(0, np.nan)
        base["mean_confidence"] = base["conf_sum"] / base["conf_n"].replace(0, np.nan)
        # ... then the derived columns exactly as today (tone_mean = tone_sum / tone_n, ...);
        # tone_n etc. are now fractional "effective n", which apply_eb already casts to float.

Which columns use the weight `w` and which stay hard
----------------------------------------------------
* USE `w` (a post contributes in proportion to its membership; totals are conserved, so
  the sum over topics of each column equals the hard total):
    - V                      = sum(w)  (V_soft; total over all topics == number of posts)
    - tone_sum / tone_n / tone_ss, hate_sum / hate_n / hate_ss / hate_hits,
      read_sum / read_n, vir_sum / vir_n / vir_ss, reach_norm_sum / reach_norm_n,
      read_cov, mean_confidence   -> weighted means; the *_n become effective n.
    - views_sum and E_sum    = sum(w * views), sum(w * E_sum)  (engagement is split, never
      duplicated, so reach_proxy of the two topics adds up to the post's reach once).
    - author_entropy / author_1_hhi: feed `S.shannon_entropy` / `S.hhi` the per-author
      WEIGHT totals (``groupby author_uid [w].sum()``) instead of row counts.
* STAY HARD (not weighted):
    - n_authors: a distinct count must not be fractional and must not be inflated by a
      w=0.1 sliver. Use the effective count ``sum_over_authors(min(1, sum_of_w))``, which
      is <= V, and also keep ``n_authors_primary`` = nunique among rows with topic_rank == 1.
      (If the lead wants plain ints: nunique over rows with w >= 0.5 only.)
    - duplicate_rate: computed on `text_norm` of rows with w >= 0.5 (majority-member rows)
      and compared with the same unweighted nunique logic; fractional duplicate counting
      has no clear meaning.
    - effort / platform_days / TS-window membership / hashtag grain: built from the hard
      `posts` (unchanged), because they are properties of the post, not of a topic.
* Downstream implications for 07 (check these when enabling count_mode="soft"):
    - V is float. NB/Poisson log-pmf through `gammaln` is fine; any ``astype(int)`` or
      integer-only gate on V must be reviewed. `support_n` / `support_min_v` compare against
      effective support: below the gate emit NULL, not 0.
    - The identity assert near the end of 07 (``int(panel_tp... V.sum()) == int(...)``)
      must become ``abs(V.sum() - n_posts) < 1e-6`` under soft mode.
    - Re-estimate NB dispersion on the soft counts (splitting lowers variance and
      correlates neighbour topics; BH remains valid under PRDS, record this in the spec).
    - Record ``count_mode``, ``soft_tau``, ``soft_m_hi`` in run_meta / metric_meta.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

UNASSIGNED = "unassigned"
_EXT_COLS = ("post_uid", "topic_id", "parent_topic_id", "topic_id_2",
             "parent_topic_id_2", "margin")


# ==========================================================================
# weights
# ==========================================================================
def _sigmoid(x: np.ndarray) -> np.ndarray:
    # sigmoid(x) = 0.5 * (1 + tanh(x / 2)): no overflow for any finite x, exactly 0.5 at 0.
    return 0.5 * (1.0 + np.tanh(0.5 * x))


def soft_weights(margin, same_parent, has_t2, tau: float, m_hi: float
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised weights for the top-2 topics.

    margin       cosine margin sim_1 - sim_2 (NaN allowed -> post stays hard)
    same_parent  bool, True when the two topics share a parent (never split)
    has_t2       bool, True when both topics exist (and differ)
    Returns (w1, w2, ambiguous). For non-ambiguous posts w1 = 1 and w2 = 0. For ambiguous
    posts w1 = sigmoid(margin / tau) in [0.5, 1) and w2 = 1 - w1, so w1 + w2 == 1 exactly.
    """
    if not (tau > 0 and m_hi > 0):
        raise ValueError(f"tau and m_hi must be > 0 (got tau={tau}, m_hi={m_hi})")
    m = np.asarray(margin, dtype=float)
    same = np.asarray(same_parent, dtype=bool)
    has2 = np.asarray(has_t2, dtype=bool)
    finite = np.isfinite(m)
    m0 = np.where(finite, np.maximum(m, 0.0), 0.0)     # margin < 0 cannot happen; clip
    amb = has2 & ~same & finite & (m0 < m_hi)
    w1 = np.where(amb, _sigmoid(m0 / tau), 1.0)
    w2 = 1.0 - w1
    return w1, w2, amb


def _exists(s: pd.Series) -> pd.Series:
    return s.notna() & (s.astype(object) != "") & (s.astype(object) != UNASSIGNED)


def pair_features(ext_df: pd.DataFrame) -> pd.DataFrame:
    """Per-row `has_t2`, `same_parent`, `cross_parent` (= has_t2 & ~same_parent).

    Index is preserved. A topic without a parent is its own parent.
    """
    t1, t2 = ext_df["topic_id"], ext_df["topic_id_2"]
    has_t2 = _exists(t1) & _exists(t2) & (t1.astype(object) != t2.astype(object))
    p1 = ext_df["parent_topic_id"].where(ext_df["parent_topic_id"].notna(), t1)
    p2 = ext_df["parent_topic_id_2"].where(ext_df["parent_topic_id_2"].notna(), t2)
    same = (p1.astype(object) == p2.astype(object))
    return pd.DataFrame({"has_t2": has_t2, "same_parent": same,
                         "cross_parent": has_t2 & ~same}, index=ext_df.index)


# ==========================================================================
# constants (first half of the window ONLY)
# ==========================================================================
def split_halves(day) -> tuple[np.ndarray, np.ndarray]:
    """Boolean (first_half, second_half) masks from a per-post day column.

    The split is at the midpoint between the earliest and latest day. Tune on the first
    mask, evaluate on the second.
    """
    d = pd.to_datetime(pd.Series(day).reset_index(drop=True))
    lo, hi = d.min(), d.max()
    mid = lo + (hi - lo) / 2
    first = (d <= mid).to_numpy()
    return first, ~first


def choose_constants(margins, parents_differ, tune_mask, q: float = 0.25,
                     w_cut: float = 0.95, min_n: int = 100) -> dict:
    """Propose TAU and M_HI from the margin distribution of the TUNING rows only.

    `tune_mask` (required, boolean, same length as `margins`) selects the first half of the
    window. Evaluation of anything built on these constants MUST use the second half.
    `parents_differ` should be ``pair_features(ext)["cross_parent"]``.

    Rule (one knob, `q`):
      M_HI = q-quantile of the margins of cross-parent tuning posts, i.e. at most a share
             `q` of the posts that COULD be split are split (default 25%).
      TAU  = M_HI / ln(w_cut / (1 - w_cut)), so w1(M_HI) = w_cut (default 0.95): the weight
             is already ~1 at the cutoff, so the hard cutoff at M_HI creates at most a
             1 - w_cut jump instead of a visible discontinuity.
    Raises ValueError if fewer than `min_n` tuning rows are cross-parent (the quantile
    would be noise) or if M_HI is not > 0.
    """
    m = np.asarray(margins, dtype=float)
    pdif = np.asarray(parents_differ, dtype=bool)
    tune = np.asarray(tune_mask, dtype=bool)
    if not (m.shape == pdif.shape == tune.shape):
        raise ValueError("margins, parents_differ and tune_mask must have the same shape")
    if not (0.0 < q < 1.0 and 0.5 < w_cut < 1.0):
        raise ValueError("need 0 < q < 1 and 0.5 < w_cut < 1")
    sel = tune & pdif & np.isfinite(m)
    n_cross = int(sel.sum())
    if n_cross < min_n:
        raise ValueError(f"only {n_cross} cross-parent tuning posts (< {min_n}); "
                         "cannot estimate constants")
    mm = np.maximum(m[sel], 0.0)
    m_hi = float(np.quantile(mm, q))
    if not m_hi > 0:
        raise ValueError("M_HI quantile is 0 (margin ties at 0); widen q")
    tau = m_hi / float(np.log(w_cut / (1.0 - w_cut)))
    qs = (0.05, 0.25, 0.5, 0.75, 0.95)
    return {
        "tau": tau, "m_hi": m_hi, "q": q, "w_cut": w_cut,
        "n_tune_rows": int(tune.sum()), "n_tune_cross_parent": n_cross,
        "tune_margin_quantiles": {f"q{int(x * 100):02d}": float(np.quantile(mm, x)) for x in qs},
    }


# ==========================================================================
# expansion
# ==========================================================================
def expand_posts(posts_df: pd.DataFrame, ext_df: pd.DataFrame, tau: float, m_hi: float
                 ) -> pd.DataFrame:
    """Long frame: original post columns, `topic_id` (1 row, or 2 for ambiguous posts),
    `topic_rank` (1 = top-1, 2 = runner-up) and float `w`.

    Weights per post_uid sum to exactly 1, so ``w.sum() == len(posts_df)``. Row order is
    deterministic: posts in input order, each post's rank-1 row then its rank-2 row.
    `posts_df.post_uid` must be unique. Posts missing from `ext_df`, with a null topic,
    or whose `topic_id` disagrees with the extension's top-1 stay hard; a null topic
    becomes ``'unassigned'``. Counts are kept in ``result.attrs["soft_topics"]``.
    """
    for c in ("post_uid", "topic_id"):
        if c not in posts_df.columns:
            raise KeyError(f"posts_df needs column {c!r}")
    missing = [c for c in _EXT_COLS if c not in ext_df.columns]
    if missing:
        raise KeyError(f"ext_df missing columns {missing}")
    if posts_df["post_uid"].duplicated().any():
        raise ValueError("posts_df.post_uid must be unique (weights are per post)")

    ext = ext_df.drop_duplicates("post_uid", keep="first").set_index("post_uid")
    feat = pair_features(ext)
    uid = posts_df["post_uid"]

    t1_post = posts_df["topic_id"].astype(object).where(posts_df["topic_id"].notna(), UNASSIGNED)
    t1_ext = uid.map(ext["topic_id"]).astype(object)
    margin = uid.map(ext["margin"]).to_numpy(dtype=float)
    t2 = uid.map(ext["topic_id_2"]).astype(object)
    has_t2 = uid.map(feat["has_t2"]).eq(True).to_numpy()
    same = uid.map(feat["same_parent"]).ne(False).to_numpy()   # unknown -> treat as same
    in_ext = uid.isin(ext.index).to_numpy()
    agree = (t1_post == t1_ext).to_numpy()
    has_t2 = has_t2 & in_ext & agree

    w1, w2, amb = soft_weights(margin, same, has_t2, tau, m_hi)

    first = posts_df.copy()
    first["topic_id"] = t1_post
    first["topic_rank"] = 1
    first["w"] = w1
    first["_ord"] = np.arange(len(first))

    second = posts_df.loc[amb & (w2 > 0)].copy()
    keep = (amb & (w2 > 0))
    second["topic_id"] = t2.to_numpy()[keep]
    second["topic_rank"] = 2
    second["w"] = w2[keep]
    second["_ord"] = np.arange(len(posts_df))[keep]

    out = pd.concat([first, second], ignore_index=True)
    out = out.sort_values(["_ord", "topic_rank"], kind="mergesort").drop(columns="_ord")
    out = out.reset_index(drop=True)
    out["w"] = out["w"].astype(float)
    out.attrs["soft_topics"] = {
        "n_posts": int(len(posts_df)),
        "n_ambiguous": int(keep.sum()),
        "n_not_in_ext": int((~in_ext).sum()),
        "n_topic_mismatch": int((in_ext & ~agree & (t1_post != UNASSIGNED)).sum()),
        "total_w": float(out["w"].sum()),
        "tau": float(tau), "m_hi": float(m_hi),
    }
    return out
