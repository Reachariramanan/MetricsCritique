#!/usr/bin/env python3
"""Offline tests for soft top-2 topic membership (synthetic data only; no DB, no DuckDB).

Run:  .venv/bin/python scripts/tests/test_soft_topics.py
  or: .venv/bin/python -m pytest scripts/tests/test_soft_topics.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import soft_topics as ST  # noqa: E402

TAU, M_HI = 0.02, 0.06


def _synthetic(n=600, seed=7):
    """posts + ext with every case: hard, cross-parent ambiguous, same-parent, unassigned."""
    rng = np.random.default_rng(seed)
    uid = [f"p{i:04d}" for i in range(n)]
    topics = [f"t{j:02d}" for j in range(12)]
    parent = {t: f"P{j // 3}" for j, t in enumerate(topics)}     # 4 parents x 3 topics
    t1 = rng.choice(topics, n)
    t2 = rng.choice(topics, n)
    margin = rng.uniform(0.0, 0.2, n)
    margin[:20] = 0.0                                            # exact ties
    t1_s = pd.Series(t1, dtype=object)
    unassigned = np.zeros(n, dtype=bool)
    unassigned[20:60] = True                                     # below_threshold posts
    t1_s[unassigned] = None
    ext = pd.DataFrame({
        "post_uid": uid, "platform": rng.choice(["a", "b", "c"], n),
        "topic_id": t1_s,
        "parent_topic_id": [parent.get(t) if t is not None else None for t in t1_s],
        "sim_1": 0.5, "topic_id_2": t2,
        "parent_topic_id_2": [parent[t] for t in t2],
        "sim_2": 0.4, "margin": margin,
        "assign_method": np.where(unassigned, "below_threshold", "hdbscan"),
        "source": "x",
    })
    posts = pd.DataFrame({
        "post_uid": uid, "platform": ext["platform"], "author_uid": rng.integers(0, 50, n),
        "day": pd.to_datetime("2026-01-01") + pd.to_timedelta(rng.integers(0, 60, n), "D"),
        "topic_id": t1_s.where(~unassigned, ST.UNASSIGNED),
        "tone_v": rng.normal(size=n),
    })
    return posts, ext, parent


def test_weights_sum_to_one_per_post():
    posts, ext, _ = _synthetic()
    out = ST.expand_posts(posts, ext, TAU, M_HI)
    s = out.groupby("post_uid")["w"].sum()
    assert len(s) == len(posts)
    assert (s == 1.0).all(), f"max |sum-1| = {np.abs(s - 1).max()}"
    assert (out["w"] > 0).all() and (out["w"] <= 1).all()
    # elementwise too, across a sweep of margins incl. extremes
    m = np.concatenate([[0.0, 1e-12, M_HI, 1.0, 50.0], np.linspace(0, 0.1, 997)])
    w1, w2, _ = ST.soft_weights(m, np.zeros(len(m), bool), np.ones(len(m), bool), TAU, 1e9)
    assert (w1 + w2 == 1.0).all() and (w1 >= 0.5).all()


def test_same_parent_never_splits():
    posts, ext, _ = _synthetic()
    feat = ST.pair_features(ext)
    assert feat["same_parent"].any() and feat["cross_parent"].any()   # fixture sanity
    out = ST.expand_posts(posts, ext, TAU, 1e9)       # m_hi huge: everything cross-parent splits
    n_rows = out.groupby("post_uid").size()
    same_uids = ext.loc[feat["has_t2"] & feat["same_parent"], "post_uid"]
    assert len(same_uids) > 0
    assert (n_rows.loc[same_uids] == 1).all()
    assert (out.loc[out["post_uid"].isin(same_uids), "w"] == 1.0).all()
    # and the cross-parent ones DO split when the cutoff allows it
    cross_uids = ext.loc[feat["cross_parent"], "post_uid"]
    assert (n_rows.loc[cross_uids] == 2).all()


def test_same_parent_zero_margin_explicit():
    ext = pd.DataFrame({"post_uid": ["x"], "topic_id": ["t1"], "parent_topic_id": ["P"],
                        "topic_id_2": ["t2"], "parent_topic_id_2": ["P"], "margin": [0.0]})
    posts = pd.DataFrame({"post_uid": ["x"], "topic_id": ["t1"]})
    out = ST.expand_posts(posts, ext, TAU, M_HI)
    assert len(out) == 1 and out["topic_id"].iloc[0] == "t1" and out["w"].iloc[0] == 1.0


def test_large_margin_is_hard():
    posts, ext, _ = _synthetic()
    big = ext.assign(margin=0.9)
    out = ST.expand_posts(posts, big, TAU, M_HI)
    assert len(out) == len(posts)
    assert (out["w"] == 1.0).all() and (out["topic_rank"] == 1).all()
    # margin just above the cutoff stays hard; just below splits with w1 ~ 0.95
    w1, w2, amb = ST.soft_weights([M_HI, M_HI * 0.999], [False, False], [True, True], TAU, M_HI)
    assert list(amb) == [False, True] and w1[0] == 1.0
    assert 0.9 < w1[1] < 1.0
    # far inside the window but with margin >> tau the weight is ~1
    w1, w2, _ = ST.soft_weights([0.5], [False], [True], 0.01, 1.0)
    assert w1[0] > 0.999999 and w2[0] < 1e-6


def test_margin_zero_is_half_half():
    ext = pd.DataFrame({"post_uid": ["x"], "topic_id": ["t1"], "parent_topic_id": ["P1"],
                        "topic_id_2": ["t4"], "parent_topic_id_2": ["P2"], "margin": [0.0]})
    posts = pd.DataFrame({"post_uid": ["x"], "topic_id": ["t1"], "author_uid": ["a"]})
    out = ST.expand_posts(posts, ext, TAU, M_HI)
    assert list(out["topic_id"]) == ["t1", "t4"]
    assert list(out["topic_rank"]) == [1, 2]
    assert list(out["w"]) == [0.5, 0.5]
    assert (out["author_uid"] == "a").all()            # original columns carried over


def test_unassigned_stays_unassigned():
    posts, ext, _ = _synthetic()
    un = ext["topic_id"].isna()
    assert un.sum() == 40
    out = ST.expand_posts(posts, ext, TAU, 1e9)
    sub = out[out["post_uid"].isin(ext.loc[un, "post_uid"])]
    assert len(sub) == 40                              # no second row, even with huge m_hi
    assert (sub["topic_id"] == "unassigned").all() and (sub["w"] == 1.0).all()
    # null topic_id in posts_df is also mapped to 'unassigned'
    p2 = posts.copy()
    p2["topic_id"] = p2["topic_id"].where(~un, None)
    out2 = ST.expand_posts(p2, ext, TAU, 1e9)
    assert (out2.loc[out2["post_uid"].isin(ext.loc[un, "post_uid"]), "topic_id"]
            == "unassigned").all()
    # no topic is ever invented: every output topic is a top-1/top-2 of some ext row
    known = set(ext["topic_id"].dropna()) | set(ext["topic_id_2"].dropna()) | {"unassigned"}
    assert set(out["topic_id"]) <= known


def test_total_weight_equals_number_of_posts():
    posts, ext, _ = _synthetic()
    for m_hi in (0.001, M_HI, 0.2, 1e9):
        out = ST.expand_posts(posts, ext, TAU, m_hi)
        assert out["w"].sum() == len(posts)
        assert out.attrs["soft_topics"]["total_w"] == len(posts)
        assert len(out) == len(posts) + out.attrs["soft_topics"]["n_ambiguous"]
    # per-topic weighted totals add back to the post count
    out = ST.expand_posts(posts, ext, TAU, M_HI)
    assert abs(out.groupby("topic_id")["w"].sum().sum() - len(posts)) < 1e-9


def test_topic_mismatch_and_missing_ext_stay_hard():
    posts, ext, _ = _synthetic()
    p2 = posts.copy()
    p2["topic_id"] = "other_basis"                      # e.g. metrics on a different topic table
    out = ST.expand_posts(p2, ext, TAU, 1e9)
    assert len(out) == len(posts) and (out["w"] == 1.0).all()
    out3 = ST.expand_posts(posts, ext.iloc[:100], TAU, 1e9)    # most posts have no ext row
    assert len(out3) < len(posts) + 100 and out3["w"].sum() == len(posts)
    assert out3.attrs["soft_topics"]["n_not_in_ext"] == len(posts) - 100


def test_deterministic():
    posts, ext, _ = _synthetic()
    a = ST.expand_posts(posts, ext, TAU, M_HI)
    b = ST.expand_posts(posts.copy(), ext.copy(), TAU, M_HI)
    pd.testing.assert_frame_equal(a, b)
    # ext row order must not matter
    c = ST.expand_posts(posts, ext.sample(frac=1.0, random_state=3), TAU, M_HI)
    pd.testing.assert_frame_equal(a, c)
    # output keeps input post order, rank-1 row before rank-2 row
    assert list(a.drop_duplicates("post_uid")["post_uid"]) == list(posts["post_uid"])


def test_choose_constants_uses_only_tuning_rows():
    posts, ext, _ = _synthetic(n=3000)
    feat = ST.pair_features(ext)
    first, second = ST.split_halves(posts["day"])
    assert first.sum() > 0 and second.sum() > 0 and not (first & second).any()
    cross = feat["cross_parent"].to_numpy()
    c1 = ST.choose_constants(ext["margin"], cross, first)
    # wreck the second half: constants must not move
    m2 = ext["margin"].to_numpy().copy()
    m2[second] = 0.0
    c2 = ST.choose_constants(m2, cross, first)
    assert c1["tau"] == c2["tau"] and c1["m_hi"] == c2["m_hi"]
    # rule: w1(M_HI) == w_cut and M_HI is the q-quantile of cross-parent tuning margins
    w1, _, _ = ST.soft_weights([c1["m_hi"] * 0.9999999], [False], [True], c1["tau"], c1["m_hi"])
    assert abs(w1[0] - 0.95) < 1e-6
    sel = first & cross
    assert abs(c1["m_hi"] - np.quantile(ext["margin"].to_numpy()[sel], 0.25)) < 1e-12
    assert c1["n_tune_cross_parent"] == int(sel.sum())
    # the tuning mask is mandatory
    try:
        ST.choose_constants(ext["margin"], cross)
    except TypeError:
        pass
    else:
        raise AssertionError("tune_mask must be required")
    # too few tuning rows -> refuse rather than return noise
    try:
        ST.choose_constants(ext["margin"], cross, np.arange(len(ext)) < 10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for tiny tuning set")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
