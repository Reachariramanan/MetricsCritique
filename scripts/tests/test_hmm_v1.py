#!/usr/bin/env python3
"""Tests for the HMM burst detector `scripts/lib/hmm_v1.py` (plan Step 2).

Simulation only -- no database, no DuckDB. Checks: recovery of known burst
episodes, posteriors are distributions, an exposure-only ingestion surge is not a
burst (while exposure-blind Kleinberg flags it), no look-ahead in the filtered
posterior, BIC picks 2 states on 2-state data, determinism, NB chosen on
overdispersed data, the stats_v2 support-gate convention, and speed.

Run:  .venv/bin/python scripts/tests/test_hmm_v1.py      (or via pytest)
"""
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import hmm_v1 as H  # noqa: E402
from lib import stats_v2 as S  # noqa: E402

N = 130
SEED = 7


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    u = int(((a == 1) | (b == 1)).sum())
    return int(((a == 1) & (b == 1)).sum()) / u if u else 1.0


def _bursty_panel(rng, n_series=20, n=N, base_lo=2.0, base_hi=12.0, lift=math.log(4.0),
                  dist="poisson", theta=5.0):
    """Series with 1-2 known episodes each and mildly varying (known) exposure."""
    xs, es, zs = [], [], []
    for i in range(n_series):
        e = np.exp(rng.normal(0, 0.15, n))  # day-to-day collection jitter
        n_ep = 1 + (i % 2)
        eps = []
        for j in range(n_ep):
            a = int(rng.integers(10 + 60 * j, 50 + 60 * j))
            eps.append((a, a + int(rng.integers(5, 11))))
        x, z = H._simulate(rng, n, float(rng.uniform(base_lo, base_hi)), lift,
                           exposure=e, episodes=eps, dist=dist, theta=theta)
        xs.append(x); es.append(e); zs.append(z)
    return xs, es, zs


def test_recovers_known_episodes():
    rng = np.random.default_rng(SEED)
    xs, es, zs = _bursty_panel(rng)
    p = H.fit_pooled(xs, es, n_states=2, seed=SEED)
    assert p.ok and p.converged, p
    assert abs(p.lift[1] - math.log(4.0)) < 0.3, p.lift
    ious = []
    for x, e, z in zip(xs, es, zs):
        br = H.hmm_burst_result(x, e, p)
        eps = S.extract_episodes(br.state, x, br.intensity)  # unchanged consumer
        assert all(0.5 < ep["level"] <= 1.0 for ep in eps)
        ious.append(_iou(br.state, z))
    assert min(ious) > 0.6 and np.mean(ious) > 0.8, ious


def test_posteriors_sum_to_one():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng, n_series=6)
    for k in (2, 3):
        p = H.fit_pooled(xs, es, n_states=k, seed=SEED)
        for i, (x, e) in enumerate(zip(xs, es)):
            g = H.smoothed_posteriors(x, e, p)
            f = H.filtered_posteriors(x, e, p, base=p.series_base[i])
            assert g.shape == (N, k) and f.shape == (N, k)
            assert np.allclose(g.sum(1), 1.0) and np.allclose(f.sum(1), 1.0)
            assert (g >= 0).all() and (f >= 0).all()


def test_ingestion_surge_is_not_a_burst():
    """Pure Poisson noise on a constant per-unit-effort rate, effort tripled for 15
    days. Kleinberg (counts only) calls the surge a burst; the HMM with the log
    offset does not."""
    rng = np.random.default_rng(SEED)
    e = np.ones(N)
    e[60:75] = 3.0
    xs = [rng.poisson(4.0 * e).astype(float) for _ in range(12)]
    p = H.fit_pooled(xs, [e] * len(xs), n_states=2, seed=SEED)
    hmm_surge, kb_surge, hmm_eps = 0, 0, 0
    for x in xs:
        br = H.hmm_burst_result(x, e, p)
        hmm_surge += int(br.state[60:75].sum())
        hmm_eps += len(S.extract_episodes(br.state, x, br.intensity))
        kb_surge += int(S.kleinberg_2state(x).state[60:75].sum())
    total = 15 * len(xs)
    assert kb_surge / total > 0.5, f"Kleinberg should flag the surge: {kb_surge}/{total}"
    assert hmm_surge / total < 0.05, f"HMM flagged surge days: {hmm_surge}/{total}"
    assert hmm_eps <= 1, hmm_eps
    # same data, exposure ignored -> the HMM does see a 'burst' (the offset is what helps)
    p_blind = H.fit_pooled(xs, None, n_states=2, seed=SEED)
    blind = sum(int(H.hmm_burst_result(x, None, p_blind).state[60:75].sum()) for x in xs)
    assert blind / total > 0.5, blind


def test_filtered_has_no_look_ahead():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng, n_series=10)
    train = 70
    p = H.fit_on_prefix(xs, es, train, n_states=2, seed=SEED)
    assert p.train_days == train
    # params depend only on the prefix: wrecking the future leaves them identical
    wrecked = [np.concatenate([x[:train], np.full(N - train, 999.0)]) for x in xs]
    assert H.fit_on_prefix(wrecked, es, train, n_states=2, seed=SEED).to_json() == p.to_json()
    for i in (0, 3, 7):
        x, e, b = xs[i], es[i], p.series_base[i]
        f = H.filtered_posteriors(x, e, p, base=b)
        for t in (0, 5, train - 1, train, 90, N - 1):
            g_trunc = H.smoothed_posteriors(x[: t + 1], e[: t + 1], p, base=b)
            assert np.allclose(f[t], g_trunc[-1], atol=1e-10), (i, t)
        mut = x.copy()
        mut[100:] = 0.0
        assert np.allclose(f[:100], H.filtered_posteriors(mut, e, p, base=b)[:100])
        rc = H.hmm_burst_result(x, e, p, causal=True, base=b)
        assert rc.method == "hmm_v1_filtered" and np.allclose(rc.intensity, f[:, 1:].sum(1))
    try:
        H.hmm_burst_result(xs[0], es[0], p, causal=True)  # no frozen base -> refuse
        raise AssertionError("causal scoring without a frozen base must raise")
    except ValueError:
        pass


def test_bic_prefers_two_states_on_two_state_data():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng)
    chosen, info = H.select_states(xs, es, seed=SEED)
    assert chosen.n_states == 2, info
    assert info["bic_2"] < info["bic_3"] + info["margin"], info
    p3 = H.fit_pooled(xs, es, n_states=3, seed=SEED)
    A3 = np.asarray(p3.trans)
    assert A3[0, 2] == 0.0 and A3[1, 0] == 0.0  # quiet->decay, active->quiet forbidden


def test_bic_prefers_three_states_on_rise_and_decay_data():
    """Sanity of the other direction: long plateaus at two distinct levels."""
    rng = np.random.default_rng(SEED)
    xs = []
    for i in range(20):
        a = 20 + 3 * i
        lam = np.full(N, 4.0)
        lam[a : a + 10] = 4.0 * 8.0   # active
        lam[a + 10 : a + 30] = 4.0 * 2.5  # decay
        xs.append(rng.poisson(lam).astype(float))
    chosen, info = H.select_states(xs, None, seed=SEED)
    assert chosen.n_states == 3, info


def test_deterministic_given_seed():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng, n_series=8)
    a = H.fit_pooled(xs, es, n_states=3, seed=11)
    b = H.fit_pooled(xs, es, n_states=3, seed=11)
    assert a.to_json() == b.to_json()
    r = H.HMMParams.from_json(a.to_json())
    assert r.to_json() == a.to_json() and r.ok


def test_negbinom_chosen_on_overdispersed_data():
    rng = np.random.default_rng(SEED)
    xs, es, zs = _bursty_panel(rng, dist="negbinom", theta=3.0, base_lo=6.0, base_hi=15.0)
    p = H.fit_pooled(xs, es, n_states=2, seed=SEED)
    assert p.dist == "negbinom" and p.dispersion > 1.5, (p.dist, p.dispersion)
    assert 1.5 < p.theta < 6.0, p.theta
    ious = [_iou(H.hmm_burst_result(x, e, p).state, z) for x, e, z in zip(xs, es, zs)]
    assert np.mean(ious) > 0.6, ious
    # soft (non-integer) counts are accepted by the gammaln emissions
    soft = [x * 0.73 for x in xs]
    ps = H.fit_pooled(soft, es, n_states=2, seed=SEED)
    assert ps.ok and np.isfinite(ps.loglik)


def test_support_gate_matches_stats_v2_convention():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng, n_series=6)
    p = H.fit_pooled(xs, es, n_states=2, seed=SEED)
    for bad in (np.array([0, 1, 0, 2, 0, 1, 0.0]),           # too short
                np.zeros(60),                                 # no volume
                np.r_[np.zeros(55), np.full(5, 4.0)]):        # mean < 0.5
        br = H.hmm_burst_result(bad, None, p)
        assert br.notes == "degenerate" and br.state.dtype.kind == "i"
        assert br.state.sum() == 0 and np.isnan(br.intensity).all()  # NULL, not 0
        assert S.extract_episodes(br.state, bad, br.intensity) == []
        assert H.detect_bursts_hmm(bad, None, p).method == "poisson_cusum"
    # degenerate params (nothing passed the gate) propagate as degenerate results
    pd_ = H.fit_pooled([np.zeros(40)] * 3, None, n_states=2)
    assert not pd_.ok and math.isinf(H.bic(pd_))
    assert H.hmm_burst_result(xs[0], es[0], pd_).notes == "degenerate"
    # sparse series in the pool are excluded, with a NaN base
    p2 = H.fit_pooled(xs + [np.zeros(N)], es + [None], n_states=2, seed=SEED)
    assert p2.n_series == len(xs) and math.isnan(p2.series_base[-1])


def test_missing_days_and_ragged_lengths():
    rng = np.random.default_rng(SEED)
    xs, es, zs = _bursty_panel(rng, n_series=10)
    ragged = [x[: N - 7 * i] for i, x in enumerate(xs)]
    rag_e = [e[: N - 7 * i] for i, e in enumerate(es)]
    holed = ragged[0].copy()
    holed[[3, 40, 41]] = np.nan
    ragged[0] = holed
    p = H.fit_pooled(ragged, rag_e, n_states=2, seed=SEED)
    assert p.ok and p.n_obs == sum(len(x) for x in ragged) - 3
    g = H.smoothed_posteriors(holed, rag_e[0], p)
    assert np.isfinite(g).all() and np.allclose(g.sum(1), 1.0)
    # padding is inert: series 0 alone gives the same posteriors as inside the pool
    g_alone = H.smoothed_posteriors(ragged[-1], rag_e[-1], p, base=p.series_base[-1])
    X, E, M, live, _ = H._pack(ragged, rag_e)
    logB = H._log_emis(X, E, M, np.asarray(p.series_base), np.asarray(p.lift), p.dist, p.theta)
    _, G, _, _ = H._forward_backward(logB, np.asarray(p.trans), np.asarray(p.init), live)
    assert np.allclose(G[-1, : len(ragged[-1])], g_alone)


def test_long_series_no_underflow():
    rng = np.random.default_rng(SEED)
    x, _ = H._simulate(rng, 8000, 300.0, math.log(2.5), episodes=[(2000, 2100), (6000, 6050)])
    p = H.fit_pooled([x], None, n_states=2, seed=SEED, max_iter=60)
    assert np.isfinite(p.loglik)
    g = H.smoothed_posteriors(x, None, p)
    assert np.isfinite(g).all() and np.allclose(g.sum(1), 1.0)
    assert g[2010:2090, 1].min() > 0.99 and g[100:1900, 1].max() < 0.01


def test_self_checks_pass():
    out = H.run_self_checks()
    failed = [k for k, v in out.items() if not v]
    assert not failed, failed


def test_speed_130_days_x_50_series():
    rng = np.random.default_rng(SEED)
    xs, es, _ = _bursty_panel(rng, n_series=50)
    t0 = time.perf_counter()
    p = H.fit_pooled(xs, es, n_states=2, seed=SEED)
    for i, (x, e) in enumerate(zip(xs, es)):
        H.hmm_burst_result(x, e, p)                                # smoothed, base re-fit
        H.hmm_burst_result(x, e, p, causal=True, base=p.series_base[i])
    dt = time.perf_counter() - t0
    assert dt < 2.0, f"{dt:.2f}s"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        t0 = time.perf_counter()
        fn()
        print(f"ok {fn.__name__}  ({time.perf_counter() - t0:.2f}s)")
