#!/usr/bin/env python3
"""Tests for the noise-gate harness (scripts/review/noise_gates.py).

A harness that cannot say no is worthless, so most of these plant a method that
SHOULD lose and assert the gate says so, and plant starved inputs and assert the
answer is INCONCLUSIVE rather than PASS. Everything is synthetic: no database,
no pipeline run.

Run:  .venv/bin/python scripts/tests/test_noise_gates.py
(pytest also collects it, but .venv does not ship pytest, so the file carries its
own tiny runner and approx/raises helpers.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "review"))

import noise_gates as G  # noqa: E402
from lib import stats_v2 as S  # noqa: E402

PASS, FAIL, INC = G.PASS, G.FAIL, G.INCONCLUSIVE


class _Approx:
    def __init__(self, v, abs=1e-6):
        self.v, self.tol = np.asarray(v, dtype=float), abs

    def __eq__(self, other):
        return bool(np.all(np.abs(np.asarray(other, dtype=float) - self.v) <= self.tol))


def approx(v, abs=1e-6):
    return _Approx(v, abs)


class raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        assert t is not None, f"expected {self.exc.__name__}"
        return issubclass(t, self.exc)


# --------------------------------------------------------------------------- #
# synthetic detectors / data
# --------------------------------------------------------------------------- #
def _result(state, c):
    return S.BurstResult("synthetic", state, state.astype(float), float(np.mean(c)), 0.0)


def noisy_detector(c, expo=None):
    """Fires on any single day above mean+0.5sd: driven by the count marginal,
    which shuffling preserves, so it cannot tell a burst from noise."""
    return _result((c > c.mean() + 0.5 * c.std()).astype(int), c)


def persistent_detector(c, expo=None):
    """Needs two consecutive days above mean+1sd, so shuffling kills most of what
    it fires on. The 'good' detector."""
    a = (c > c.mean() + c.std()).astype(int)
    st = a.copy()
    st[1:] = a[1:] * a[:-1]
    st[:-1] |= st[1:]
    return _result(st, c)


_CACHE: dict = {}


def _burst_series():
    if "b" in _CACHE:
        return _CACHE["b"]
    rng = np.random.default_rng(1)
    out = []
    for _ in range(20):
        x = rng.poisson(5, 40).astype(float)
        x[15:20] = rng.poisson(20, 5)           # one planted contiguous burst
        out.append(x)
    _CACHE["b"] = out
    return out


# --------------------------------------------------------------------------- #
# 1. burst null ratio
# --------------------------------------------------------------------------- #
def test_good_detector_beats_noisy_adopt():
    burst_series = _burst_series()
    g = G.compare_burst_null(burst_series, noisy_detector, persistent_detector, n_perm=100)
    assert g["candidate_ratio"] > g["legacy_ratio"]
    assert g["status"] == PASS, g["reason"]
    assert G.verdict({"burst_null": g}, required=["burst_null"])["overall"] == G.ADOPT


def test_noisy_candidate_vs_good_legacy_is_rejected():
    burst_series = _burst_series()
    g = G.compare_burst_null(burst_series, persistent_detector, noisy_detector, n_perm=100)
    assert g["status"] == FAIL, g["reason"]
    v = G.verdict({"burst_null": g}, required=["burst_null"])
    assert v["overall"] == G.DO_NOT_ADOPT and "burst_null" in v["blocking"]


def test_identical_detectors_do_not_fall():
    burst_series = _burst_series()
    g = G.compare_burst_null(burst_series, noisy_detector, noisy_detector, n_perm=50)
    assert g["diff"] == 0.0 and g["status"] == PASS


def test_shuffles_identical_across_detectors():
    burst_series = _burst_series()
    a = G.burst_null_ratio(burst_series[:3], noisy_detector, n_perm=30, seed=5)
    b = G.burst_null_ratio(burst_series[:3], noisy_detector, n_perm=30, seed=5)
    pd.testing.assert_frame_equal(a["per_series"], b["per_series"])


def test_exposure_is_passed_and_shuffled_jointly():
    seen = []

    def det(c, e):
        seen.append((c.copy(), None if e is None else e.copy()))
        return _result(np.zeros(c.size, int), c)

    x = np.arange(10.0)
    G.burst_null_ratio([(x, x * 2)], det, n_perm=3, seed=0)
    assert all(e is not None and np.allclose(e, 2 * c) for c, e in seen)


def test_burst_gate_inconclusive_on_few_series():
    burst_series = _burst_series()
    g = G.compare_burst_null(burst_series[:3], noisy_detector, persistent_detector, n_perm=50)
    assert g["status"] == INC


def test_burst_gate_inconclusive_when_nothing_fires():
    quiet = [np.full(30, 3.0)] * 12
    g = G.compare_burst_null(quiet, noisy_detector, persistent_detector, n_perm=20)
    assert g["status"] == INC, "no episodes anywhere is absence of evidence, not a pass"


# --------------------------------------------------------------------------- #
# 2. chain null
# --------------------------------------------------------------------------- #
def test_chain_null():
    null = np.random.default_rng(0).poisson(10, 500)
    assert G.chain_null_gate(40, null)["status"] == PASS
    assert G.chain_null_gate(11, null)["status"] == FAIL
    assert G.chain_null_gate(40, null[:20])["status"] == INC
    assert G.chain_null_gate(2, null)["status"] == INC


# --------------------------------------------------------------------------- #
# 3. stability
# --------------------------------------------------------------------------- #
def _eps(rows):
    return pd.DataFrame(rows, columns=["topic_id", "platform", "start_day", "end_day"])


def test_episode_stability_known_values():
    a = _eps([("t1", "x", 0, 3), ("t2", "x", 10, 11)])
    assert G.episode_stability(a, a)["mean_jaccard"] == 1.0
    b = _eps([("t1", "x", 2, 5), ("t2", "x", 10, 11)])          # t1: {2,3}/{0..5} = 2/6
    r = G.episode_stability(a, b)
    assert r["mean_jaccard"] == approx((2 / 6 + 1.0) / 2)
    # a key that exists on one side only scores 0 and is not dropped
    c = _eps([("t1", "x", 0, 3)])
    r = G.episode_stability(a, c)
    assert r["n_keys"] == 2 and r["n_matched"] == 1
    assert r["mean_jaccard"] == approx(0.5)
    # dates work too, and multiple episodes per key merge into one day-set
    d = _eps([("t", "p", pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")),
              ("t", "p", pd.Timestamp("2026-07-05"), pd.Timestamp("2026-07-05"))])
    e = _eps([("t", "p", pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02"))])
    assert G.episode_stability(d, e)["mean_jaccard"] == approx(2 / 3)


def _perturbation_posts():
    rng = np.random.default_rng(3)
    rows = []
    n_topics = 12
    for t in range(n_topics):
        for day in range(30):
            n = 8 if 10 <= day <= 14 else 1                    # a 5-day burst per topic
            for _ in range(n):
                rows.append({"topic_id": f"t{t}", "platform": "x", "day": day,
                             "runner_up_topic_id": f"t{(t + 1) % n_topics}",
                             "ambiguous": bool(rng.random() < 0.5)})
    return pd.DataFrame(rows)


def _make_rebuild(min_count):
    def rebuild(posts):
        out = []
        cnt = posts.groupby(["topic_id", "platform", "day"]).size().rename("n").reset_index()
        for (t, p), g in cnt[cnt["n"] >= min_count].groupby(["topic_id", "platform"]):
            days = sorted(g["day"])
            s = prev = days[0]
            for d in days[1:] + [None]:
                if d is None or d != prev + 1:
                    out.append((t, p, int(s), int(prev)))
                    s = d
                prev = d
        return _eps(out)
    return rebuild


def test_label_perturbation_separates_robust_from_fragile():
    posts = _perturbation_posts()
    robust = G.label_perturbation(posts, _make_rebuild(4), seed=1)
    fragile = G.label_perturbation(posts, _make_rebuild(8), seed=1)
    assert robust["n_flipped"] >= G.MIN_FLIPPED_POSTS
    assert robust["mean_jaccard"] > fragile["mean_jaccard"]
    assert G.stability_gate(fragile, robust)["status"] == PASS
    assert G.stability_gate(robust, fragile)["status"] == FAIL
    # deterministic, and posts are not mutated
    before = posts["topic_id"].copy()
    again = G.label_perturbation(posts, _make_rebuild(8), seed=1)
    assert again["per_rep"] == fragile["per_rep"]
    assert posts["topic_id"].equals(before)


def test_stability_inconclusive_when_starved():
    posts = _perturbation_posts()
    tiny = G.label_perturbation(posts[posts["topic_id"].isin(["t0", "t1"])],
                                _make_rebuild(4), seed=1)
    assert tiny["n_keys"] < G.MIN_STABILITY_KEYS
    assert G.stability_gate(tiny, tiny)["status"] == INC


# --------------------------------------------------------------------------- #
# 4. restatement
# --------------------------------------------------------------------------- #
def test_restatement_gate_rules():
    rng = np.random.default_rng(11)
    N = 200
    V = rng.integers(1, 400, N).astype(float)
    df = pd.DataFrame({"V": V, "indep": rng.normal(size=N), "copy": np.log1p(V) ** 3,
                       "mid": 0.02 * V + rng.normal(0, 4, N)})
    r = G.restatement_gate(df, "V", ["indep", "copy"], {"indep": 0.3, "copy": 0.3})
    assert r["per_col"]["copy"]["status"] == FAIL and r["status"] == FAIL
    assert G.restatement_gate(df, "V", ["indep"], {"indep": 0.3})["status"] == PASS
    # must not exceed legacy even when under 0.7
    rho = r["per_col"]["indep"]["rho"]
    mid = G.restatement_gate(df, "V", ["mid"], {"mid": 0.05})
    assert mid["status"] == FAIL and "legacy" in mid["per_col"]["mid"]["reason"]
    assert abs(rho) < 0.3
    # no legacy value: absolute rule only, and it says so
    nl = G.restatement_gate(df, "V", ["indep"], {})
    assert nl["per_col"]["indep"]["rule"] == "absolute_only"


def test_restatement_uses_spearman_not_pearson():
    rng = np.random.default_rng(11)
    N = 200
    heavy = np.concatenate([rng.normal(0, 1, N - 5), [80, 90, 100, 110, 120]])
    Vh = np.concatenate([rng.integers(1, 20, N - 5).astype(float), [900.0] * 5])
    df = pd.DataFrame({"V": Vh, "m": heavy})
    pear = float(np.corrcoef(df["V"], df["m"])[0, 1])
    r = G.restatement_gate(df, "V", ["m"], {"m": 0.9})
    assert pear > G.RESTATEMENT_RHO_CEIL, "construction: Pearson must be inflated"
    assert abs(r["per_col"]["m"]["rho"]) < G.RESTATEMENT_RHO_CEIL
    assert r["status"] == PASS, "a Pearson-based gate would have failed this"


def test_restatement_inconclusive_when_starved():
    df = pd.DataFrame({"V": np.arange(10.0), "m": np.arange(10.0) % 3})
    assert G.restatement_gate(df, "V", ["m"], {"m": 0.5})["status"] == INC
    big = pd.DataFrame({"V": np.arange(100.0), "m": np.ones(100)})
    assert G.restatement_gate(big, "V", ["m"], {"m": 0.5})["status"] == INC
    assert G.restatement_gate(big, "V", ["absent"], {})["status"] == INC


# --------------------------------------------------------------------------- #
# 5. D5 self-test
# --------------------------------------------------------------------------- #
def test_selftest_gate():
    assert G.selftest_gate({"momentum": 1.0, "surprise": 1.0})["status"] == PASS
    assert G.selftest_gate({"momentum": 0.999})["status"] == FAIL
    assert G.selftest_gate({"momentum": float("nan")})["status"] == INC
    assert G.selftest_gate({})["status"] == INC


# --------------------------------------------------------------------------- #
# 6. half split / sampling / precision
# --------------------------------------------------------------------------- #
def test_half_split_disjoint_complete_chronological():
    days = pd.to_datetime(["2026-07-01"] * 3 + ["2026-07-02", "2026-07-03", "2026-07-03",
                                                "2026-07-04", "2026-07-05"])
    a, b = G.half_split(days)
    assert not (a & b).any() and (a | b).all()
    assert days[a].max() < days[b].min(), "first half must precede second half"
    assert a.sum() > 0 and b.sum() > 0
    # a day never straddles the split (both 07-03 rows land on the same side)
    assert len(set(b[[4, 5]])) == 1
    with raises(ValueError):
        G.half_split(pd.Series([pd.Timestamp("2026-07-01"), pd.NaT, pd.Timestamp("2026-07-03")]))
    with raises(ValueError):
        G.half_split([1, 1, 1])


def _items():
    rng = np.random.default_rng(2)
    rows = []
    for m in ("legacy", "candidate"):
        for i in range(600):
            rows.append({"post_id": f"p{i % 450}", "method": m,
                         "platform": rng.choice(["a", "b", "c"], p=[0.6, 0.3, 0.1]),
                         "label_class": rng.choice(["x", "y"]),
                         "text": f"post {i}"})
    return pd.DataFrame(rows)


def test_sampling_plan_reproducible_blind_stratified():
    df = _items()
    s1, k1 = G.sampling_plan(df, 120, ["platform"], seed=9)
    s2, k2 = G.sampling_plan(df, 120, ["platform"], seed=9)
    pd.testing.assert_frame_equal(s1, s2)
    pd.testing.assert_frame_equal(k1, k2)
    s3, _ = G.sampling_plan(df, 120, ["platform"], seed=10)
    assert not s1["text"].equals(s3["text"]), "a different seed must give a different sample"
    assert "method" not in s1.columns, "source method must be hidden"
    assert not any("legacy" in str(v) or "candidate" in str(v) for v in s1.to_numpy().ravel())
    assert s1["label"].isna().all()
    assert len(s1) == 240 and (k1["method"].value_counts() == 120).all()
    assert s1["item_id"].is_unique and list(s1["item_id"]) == sorted(s1["item_id"])
    # ids carry no method information: method sequence is not blocky
    seq = k1["method"].to_numpy()
    assert (seq[:-1] != seq[1:]).sum() > 20
    # the smallest stratum (10% platform c) is represented, not rounded to zero
    j = k1.merge(s1[["item_id", "platform"]], on="item_id")
    assert (j.groupby("method")["platform"].nunique() == 3).all()
    # un-blinding recovers the right rows
    key_by_id = k1.set_index("item_id")["method"]
    ref = df.set_index(["text", "method"])
    for _, r in s1.head(20).iterrows():
        assert (r["text"], key_by_id[r["item_id"]]) in ref.index


def test_sampling_plan_item_col_judges_shared_items_once():
    df = _items()
    sheet, key = G.sampling_plan(df, 120, ["platform"], seed=1, item_col="post_id")
    assert sheet["post_id"].is_unique
    assert len(key) == 240 and len(sheet) <= 240
    assert set(key["item_id"]) == set(sheet["item_id"])


def test_sampling_plan_small_input_takes_all():
    df = _items().head(30)
    sheet, key = G.sampling_plan(df, 120, ["platform"], seed=1)
    assert len(sheet) == 30 == len(key)


def test_wilson_known_values():
    lo, hi = G.wilson_ci(8, 10)                       # textbook: 0.4902, 0.9433
    assert (lo, hi) == approx((0.4902, 0.9433), abs=5e-4)
    lo, hi = G.wilson_ci(0, 10)                       # 0.0, 0.2775
    assert lo == 0.0 and hi == approx(0.2775, abs=5e-4)
    lo, hi = G.wilson_ci(10, 10)
    assert hi == approx(1.0, abs=1e-9) and lo == approx(0.7225, abs=5e-4)
    assert G.wilson_ci(0, 0) == (0.0, 1.0)
    try:
        from statsmodels.stats.proportion import proportion_confint
        for k, n in [(3, 7), (60, 120), (1, 50)]:
            assert G.wilson_ci(k, n) == approx(
                proportion_confint(k, n, method="wilson"), abs=1e-9)
    except ImportError:
        pass


def test_judge_precision_counts_unjudged_separately():
    r = G.judge_precision([True] * 90 + [False] * 30 + [None, float("nan")])
    assert (r["n"], r["k"], r["n_unjudged"]) == (120, 90, 2)
    assert r["precision"] == approx(0.75)
    assert r["ci_lo"] < 0.75 < r["ci_hi"]
    assert np.isnan(G.judge_precision([])["precision"])


def test_precision_by_method_unblinds():
    df = _items()
    sheet, key = G.sampling_plan(df, 120, ["platform"], seed=4)
    sheet["label"] = [True if i % 2 else False for i in range(len(sheet))]
    out = G.precision_by_method(sheet, key)
    assert set(out) == {"legacy", "candidate"} and out["legacy"]["n"] == 120


def test_precision_gate():
    good = G.judge_precision([True] * 100 + [False] * 20)
    bad = G.judge_precision([True] * 40 + [False] * 80)
    ok = G.judge_precision([True] * 96 + [False] * 24)
    assert G.precision_gate(good, ok)["status"] == PASS
    assert G.precision_gate(good, bad)["status"] == FAIL
    worse = G.judge_precision([True] * 75 + [False] * 45)
    assert G.precision_gate(good, worse)["status"] == FAIL
    # point estimate above the floor but CI straddles it -> cannot call it
    edge = G.judge_precision([True] * 33 + [False] * 27)
    assert edge["ci_lo"] < G.PRECISION_FLOOR <= edge["precision"]
    assert G.precision_gate(edge, edge)["status"] == INC
    tiny = G.judge_precision([True] * 19 + [False])
    assert G.precision_gate(good, tiny)["status"] == INC, "95% on n=20 is not a PASS"


# --------------------------------------------------------------------------- #
# 7. alert churn
# --------------------------------------------------------------------------- #
def _alerts(spec):
    return pd.DataFrame(spec, columns=["topic_id", "platform", "day", "severity"])


def test_alert_churn_and_gate():
    leg = _alerts([("t1", "x", 1, "high"), ("t2", "x", 1, "medium"), ("t3", "x", 2, "high"),
                   ("t4", "x", 2, "medium")] + [(f"k{i}", "x", 3, "medium") for i in range(10)])
    new = _alerts([("t1", "x", 1, "high"), ("t2", "x", 1, "high"), ("t5", "x", 2, "high"),
                   ("t6", "x", 2, "medium")] + [(f"k{i}", "x", 3, "medium") for i in range(10)])
    c = G.alert_churn(leg, new)
    assert (c["n_added"], c["n_removed"], c["n_kept"]) == (2, 2, 12)
    assert c["n_severity_changed"] == 1
    hc = c["high_changes"]
    assert set(hc["change"]) == {"added", "removed", "severity_changed"}
    assert set(hc["topic_id"]) == {"t2", "t3", "t5"}        # t4/t6 are medium: not required
    assert G.churn_gate(c, {})["status"] == FAIL
    keys = [(r.topic_id, r.platform, r.day) for r in hc.itertuples()]
    assert G.churn_gate(c, {k: "explained" for k in keys})["status"] == PASS
    assert G.churn_gate(c, {k: "  " for k in keys})["status"] == FAIL, "blank is not an explanation"


def test_alert_churn_keyed_on_topic_not_collapsed_by_metric():
    leg = _alerts([("t1", "x", 1, "medium"), ("t1", "x", 1, "high")])   # two metrics, same key
    c = G.alert_churn(leg, leg)
    assert c["n_legacy"] == 1 and c["n_kept"] == 1 and len(c["high_changes"]) == 0


def test_churn_inconclusive_on_tiny_alert_sets():
    a = _alerts([("t1", "x", 1, "high")])
    assert G.churn_gate(G.alert_churn(a, a), {})["status"] == INC


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def test_verdict_inconclusive_never_passes():
    allp = {g: {"status": PASS, "reason": "ok"} for g in G.ALL_GATES}
    v = G.verdict(allp)
    assert v["overall"] == G.ADOPT and not v["blocking"]
    one = {**allp, "precision": {"status": INC, "reason": "n=20"}}
    v = G.verdict(one)
    assert v["overall"] == G.DO_NOT_ADOPT and v["blocking"] == {"precision": INC}
    assert v["blocking_reasons"]["precision"] == "n=20"
    miss = {k: allp[k] for k in G.ALL_GATES if k != "chain_null"}
    v = G.verdict(miss)
    assert v["gates"]["chain_null"] == INC and v["overall"] == G.DO_NOT_ADOPT
    assert G.verdict(miss, required=[g for g in G.ALL_GATES if g != "chain_null"])["overall"] == G.ADOPT
    assert G.verdict({**allp, "stability": FAIL})["blocking"] == {"stability": FAIL}
    # bare strings accepted; extras are informational only
    v = G.verdict({"burst_null": PASS, "other": FAIL}, required=["burst_null"])
    assert v["overall"] == G.ADOPT and v["informational"] == {"other": FAIL}


def test_end_to_end_adopt_and_reject():
    burst_series = _burst_series()
    """Compose real gate outputs the way a review script would."""
    def run(legacy, cand):
        rng = np.random.default_rng(0)
        N = 150
        V = rng.integers(1, 300, N).astype(float)
        df = pd.DataFrame({"V": V, "m": rng.normal(size=N)})
        sheet_l = G.judge_precision([True] * 96 + [False] * 24)
        sheet_c = G.judge_precision([True] * 100 + [False] * 20)
        a = _alerts([(f"t{i}", "x", i, "medium") for i in range(30)])
        return G.verdict({
            "burst_null": G.compare_burst_null(burst_series, legacy, cand, n_perm=100),
            "restatement": G.restatement_gate(df, "V", ["m"], {"m": 0.3}),
            "precision": G.precision_gate(sheet_l, sheet_c),
            "alert_churn": G.churn_gate(G.alert_churn(a, a), {}),
        }, required=["burst_null", "restatement", "precision", "alert_churn"])

    assert run(noisy_detector, persistent_detector)["overall"] == G.ADOPT
    assert run(persistent_detector, noisy_detector)["overall"] == G.DO_NOT_ADOPT


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted((k, v) for k, v in globals().items()
                           if k.startswith("test_") and callable(v)):
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL  {name}: {e!r}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
