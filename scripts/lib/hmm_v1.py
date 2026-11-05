"""HMM burst detector (Metrics v2, burst_method = "hmm").

Replacement candidate for the fixed-parameter Kleinberg Viterbi in
`stats_v2.kleinberg_2state`. Kleinberg fixes the burst rate at ``s * mean`` and the
entry cost at ``gamma * ln n``, ignores collection effort, and returns a hard path.
This module instead *fits* a count HMM, pools its parameters across series (e.g.
per platform), carries a log-exposure offset so ingestion surges are explained by
effort rather than flagged, and returns posteriors (smoothed for retrospective
episodes, filtered for causal alerting). Output is a `stats_v2.BurstResult`, so
`stats_v2.extract_episodes` and the onset/decay fits run on it unchanged.

Model
-----
Series ``s``, day ``t``, state ``k``::

    x[s,t] | z=k  ~  Poisson(mu)  or  NB2(mu, theta)        (Var = mu + mu^2/theta)
    log mu[s,t,k] = log base[s] + log exposure[s,t] + lift[k]

* ``lift[0] = 0`` (quiet) by identifiability; burst states have
  ``lift[k] >= min_log_lift`` (default ln 2, i.e. Kleinberg's s=2 becomes a floor,
  not a fixed value). Without a floor an HMM fitted to pure noise happily splits
  Poisson scatter into two near-identical states and the posterior flickers.
* ``base[s]`` is a per-series nuisance rate (quiet-state count per unit exposure).
  Topics differ by orders of magnitude, so a pooled level would be meaningless; the
  *shape* parameters (lifts, theta, transitions, initial distribution) are pooled.
* ``theta`` (NB only) is shared across states and series.

Transition matrix (rows = from, cols = to; ``.`` = structural zero, kept exactly 0
by Baum-Welch because a zero entry never accumulates expected transitions)::

    n_states=2  (0 quiet, 1 active)        n_states=3  (0 quiet, 1 active, 2 decay)
            q    a                                 q    a    d
        q [ *    * ]                           q [ *    *    . ]
        a [ *    * ]                           a [ .    *    * ]
                                               d [ *    *    * ]

For 3 states a burst must pass through decay before returning to quiet; decay may
re-ignite to active (secondary waves) but quiet cannot jump straight to decay. Both
active and decay count as "burst" (state 1 in the BurstResult). No ordering of
lift[active] vs lift[decay] is enforced: if EM swaps their roles the burst/quiet
split -- the only thing exported -- is unaffected.

Numerics: emissions are evaluated in log space, shifted by their per-day max before
exponentiation, and the forward/backward recursions are per-step normalised (Rabiner
scaling). Log-likelihood is accumulated from the scale factors, so it does not
underflow on long series. Series are padded into one (S, T) matrix and the
recursions are vectorised across series; padded or missing days get an
uninformative emission (log p = 0), which provably leaves real-day posteriors
unchanged.

No look-ahead: `filtered_posteriors` uses only ``x[:t+1]`` with frozen parameters
and a frozen per-series base. Fit those on a training prefix (`fit_on_prefix`);
`run_self_checks` proves filtered(t) == smoothed(truncated at t) and that mutating
the future does not move the past.

Reference: Rabiner, "A Tutorial on Hidden Markov Models" (Proc. IEEE 1989) for
scaled forward-backward; the exposure offset is the standard Poisson/NB GLM offset.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Optional, Sequence

import numpy as np
from scipy import optimize, special

try:  # imported as `lib.hmm_v1` from scripts/, or as `hmm_v1` with lib/ on sys.path
    from lib import stats_v2 as S
except ImportError:  # pragma: no cover
    import stats_v2 as S  # type: ignore

__all__ = [
    "HMM_SEED",
    "HMMParams",
    "hmm_support_ok",
    "fit_pooled",
    "fit_on_prefix",
    "fit_series_base",
    "select_states",
    "bic",
    "smoothed_posteriors",
    "filtered_posteriors",
    "hmm_burst_result",
    "detect_bursts_hmm",
    "run_self_checks",
]

HMM_SEED = 20261027

# Support gates: identical defaults to `stats_v2.detect_bursts`' Kleinberg gate, so a
# series the HMM refuses is exactly a series Kleinberg would also have refused.
MIN_DAYS = 21
MIN_TOTAL = 30
MIN_MEAN = 0.5

_EPS = 1e-12
_B_FLOOR = 1e-300        # caps a single day's emission deficit at ~690 nats vs the best state
_TRANS_FLOOR = 1e-6      # allowed transitions never collapse to an exact (absorbing) 0
_THETA_BOUNDS = (0.1, 1e4)  # above 1e4 NB is Poisson in all but name (stats_v2 rule)
_DISPERSION_TRIGGER = 1.5   # posterior Pearson dispersion above which NB is also tried

_ALLOWED = {
    2: np.array([[1, 1], [1, 1]], dtype=bool),
    3: np.array([[1, 1, 0], [0, 1, 1], [1, 1, 1]], dtype=bool),
}
_INIT_TRANS = {
    2: np.array([[0.95, 0.05], [0.20, 0.80]]),
    3: np.array([[0.95, 0.05, 0.0], [0.0, 0.70, 0.30], [0.25, 0.05, 0.70]]),
}
_INIT_LIFT = {2: np.array([0.0, math.log(3.0)]), 3: np.array([0.0, math.log(4.0), math.log(2.0)])}


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HMMParams:
    """Pooled, frozen HMM parameters (JSON-serialisable via `to_dict`/`to_json`).

    ``series_base`` holds the per-series quiet rate fitted alongside, in the order
    of the ``series_list`` given to the fit (NaN for series that failed the
    support gate). Pass ``base=params.series_base[i]`` for causal scoring.
    ``notes == "degenerate"`` means nothing could be fitted.
    """

    n_states: int
    dist: str                                  # 'poisson' | 'negbinom'
    lift: tuple[float, ...]                    # log rate multiplier per state, lift[0]=0
    theta: float                               # NB size; NaN for poisson
    trans: tuple[tuple[float, ...], ...]       # row-stochastic K x K
    init: tuple[float, ...]
    loglik: float
    n_obs: int                                 # observed (non-missing) series-days
    n_series: int                              # series that passed the gate
    n_iter: int
    converged: bool
    seed: int
    dispersion: float                          # posterior Pearson dispersion of the Poisson fit
    min_log_lift: float
    train_days: Optional[int] = None           # set by fit_on_prefix
    series_base: tuple[float, ...] = ()
    pool_key: str = ""
    notes: str = ""

    @property
    def n_free(self) -> int:
        """Free parameters for BIC (pooled shape + one base per fitted series)."""
        k = self.n_states
        n_trans = int(sum(int(r.sum()) - 1 for r in _ALLOWED[k]))
        return (k - 1) + n_trans + (k - 1) + (1 if self.dist == "negbinom" else 0) + self.n_series

    @property
    def ok(self) -> bool:
        return self.notes != "degenerate" and np.isfinite(self.loglik)

    def to_dict(self) -> dict:
        def clean(v):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            if isinstance(v, (list, tuple)):
                return [clean(u) for u in v]
            return v
        return {k: clean(v) for k, v in asdict(self).items()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "HMMParams":
        def f(v):
            return float("nan") if v is None else float(v)
        return cls(
            n_states=int(d["n_states"]), dist=str(d["dist"]),
            lift=tuple(f(v) for v in d["lift"]), theta=f(d["theta"]),
            trans=tuple(tuple(f(v) for v in r) for r in d["trans"]),
            init=tuple(f(v) for v in d["init"]), loglik=f(d["loglik"]),
            n_obs=int(d["n_obs"]), n_series=int(d["n_series"]), n_iter=int(d["n_iter"]),
            converged=bool(d["converged"]), seed=int(d["seed"]),
            dispersion=f(d["dispersion"]), min_log_lift=f(d["min_log_lift"]),
            train_days=None if d.get("train_days") is None else int(d["train_days"]),
            series_base=tuple(f(v) for v in d.get("series_base", ())),
            pool_key=str(d.get("pool_key", "")), notes=str(d.get("notes", "")),
        )

    @classmethod
    def from_json(cls, s: str) -> "HMMParams":
        return cls.from_dict(json.loads(s))


def bic(params: HMMParams) -> float:
    """BIC = -2 LL + p ln N, N = observed series-days."""
    if not params.ok or params.n_obs <= 0:
        return float("inf")
    return -2.0 * params.loglik + params.n_free * math.log(params.n_obs)


# --------------------------------------------------------------------------- #
# Data preparation
# --------------------------------------------------------------------------- #
def hmm_support_ok(
    counts: Sequence[float],
    *,
    min_days: int = MIN_DAYS,
    min_total: float = MIN_TOTAL,
    min_mean: float = MIN_MEAN,
) -> tuple[bool, str]:
    """Series-level gate (same thresholds as Kleinberg's in `detect_bursts`)."""
    x = np.asarray(counts, dtype=float)
    n = x.size
    obs = x[np.isfinite(x)]
    tot = float(obs.sum()) if obs.size else 0.0
    mean = float(obs.mean()) if obs.size else 0.0
    ok = n >= min_days and tot >= min_total and mean >= min_mean
    return ok, f"series n={n} total={tot:.0f} mean={mean:.2f}"


def _as_exposure(exposure: Optional[Sequence[float]], n: int) -> np.ndarray:
    if exposure is None:
        return np.ones(n)
    e = np.asarray(exposure, dtype=float)
    if e.size != n:
        raise ValueError("exposure must match counts length")
    return e


def _pack(series_list: Sequence[Sequence[float]], exposure_list) -> tuple:
    """Pad to (S, T). Missing counts / non-positive or NaN exposure -> masked day."""
    S_ = len(series_list)
    lens = np.array([len(x) for x in series_list], dtype=int)
    T = int(lens.max()) if S_ else 0
    X = np.zeros((S_, T))
    E = np.ones((S_, T))
    M = np.zeros((S_, T), dtype=bool)
    for i, x in enumerate(series_list):
        x = np.asarray(x, dtype=float)
        e = _as_exposure(None if exposure_list is None else exposure_list[i], x.size)
        ok = np.isfinite(x) & (x >= 0) & np.isfinite(e) & (e > 0)
        X[i, : x.size] = np.where(ok, x, 0.0)
        E[i, : x.size] = np.where(ok, e, 1.0)
        M[i, : x.size] = ok
    live = np.arange(T)[None, :] < lens[:, None]  # inside the series' own length
    return X, E, M, live, lens


# --------------------------------------------------------------------------- #
# Emissions and forward-backward
# --------------------------------------------------------------------------- #
def _log_emis(X, E, M, base, lift, dist, theta) -> np.ndarray:
    """(S, T, K) log p(x | state); masked days are 0 (uninformative)."""
    mu = base[:, None, None] * E[:, :, None] * np.exp(np.asarray(lift))[None, None, :]
    mu = np.maximum(mu, _EPS)
    x = X[:, :, None]
    if dist == "poisson":
        lp = x * np.log(mu) - mu - special.gammaln(x + 1.0)
    else:
        th = float(theta)
        lp = (special.gammaln(x + th) - special.gammaln(th) - special.gammaln(x + 1.0)
              + th * (math.log(th) - np.log(th + mu)) + x * (np.log(mu) - np.log(th + mu)))
    return np.where(M[:, :, None], lp, 0.0)


def _forward_backward(logB, A, pi, live, *, backward=True):
    """Scaled forward(-backward), vectorised over series.

    Returns (alpha, gamma, xi_sum, loglik_per_series). ``alpha[s,t]`` is the
    filtered posterior p(z_t | x_{:t}); ``gamma`` the smoothed one. Padded days
    (outside ``live``) have logB = 0 so their scale factor is exactly 1.
    """
    S_, T, K = logB.shape
    mx = logB.max(axis=2, keepdims=True)
    B = np.maximum(np.exp(logB - mx), _B_FLOOR)
    alpha = np.empty((S_, T, K))
    c = np.empty((S_, T))
    a = pi[None, :] * B[:, 0]
    c[:, 0] = a.sum(1)
    alpha[:, 0] = a / c[:, :1]
    for t in range(1, T):
        a = (alpha[:, t - 1] @ A) * B[:, t]
        c[:, t] = a.sum(1)
        alpha[:, t] = a / c[:, t : t + 1]
    ll = np.log(c).sum(1) + mx[:, :, 0].sum(1)
    if not backward:
        return alpha, None, None, ll
    beta = np.empty((S_, T, K))
    beta[:, T - 1] = 1.0
    for t in range(T - 2, -1, -1):
        beta[:, t] = ((B[:, t + 1] * beta[:, t + 1]) @ A.T) / c[:, t + 1 : t + 2]
    gamma = alpha * beta
    gamma /= gamma.sum(2, keepdims=True)
    # expected transitions t-1 -> t, only where day t is inside the series
    if T > 1:
        w = (B[:, 1:] * beta[:, 1:]) / c[:, 1:, None]                 # (S, T-1, K)
        w = np.where(live[:, 1:, None], w, 0.0)
        xi_sum = np.einsum("sti,stj->ij", alpha[:, :-1], w) * A
    else:
        xi_sum = np.zeros((K, K))
    return alpha, gamma, xi_sum, ll


# --------------------------------------------------------------------------- #
# Baum-Welch (ECM: one conditional update of each block per iteration)
# --------------------------------------------------------------------------- #
# Ceiling on the burst-state rate multiplier (20x). Without it, series with many zero days let
# the quiet state collapse onto the zeros and the 'burst' state absorb every non-zero day
# (fitted lifts of e^6..e^11 seen on v1 topics), so 'burst' meant 'the topic had any posts'.
MAX_LOG_LIFT = math.log(20.0)


def _m_step_rates(X, E, M, G, base, lift, dist, theta, min_log_lift):
    """Update per-series base then pooled lifts, holding the other fixed.

    Poisson updates are exact conditional maxima. NB uses the IRLS fixed point
    m <- sum g*x*w / sum g*c*w with w = theta/(theta+mu) at the current mu.
    """
    Gm = G * M[:, :, None]
    el = np.exp(lift)
    if dist == "poisson":
        w = np.ones_like(Gm)
    else:
        mu = base[:, None, None] * E[:, :, None] * el[None, None, :]
        w = theta / (theta + mu)
    num_b = np.einsum("stk,st->s", Gm * w, X)
    den_b = np.einsum("stk,st,k->s", Gm * w, E, el)
    base = np.where(den_b > 0, num_b / np.maximum(den_b, _EPS), base)
    base = np.maximum(base, _EPS)
    if dist != "poisson":
        mu = base[:, None, None] * E[:, :, None] * el[None, None, :]
        w = theta / (theta + mu)
    num_l = np.einsum("stk,st->k", Gm * w, X)
    den_l = np.einsum("stk,st,s->k", Gm * w, E, base)
    new = lift.copy()
    for k in range(1, lift.size):
        if den_l[k] > 0 and num_l[k] > 0:
            new[k] = math.log(num_l[k] / den_l[k])
        new[k] = min(max(new[k], min_log_lift), MAX_LOG_LIFT)
    new[0] = 0.0
    return base, new


def _m_step_theta(X, E, M, G, base, lift, theta):
    Gm = G * M[:, :, None]
    mu = np.maximum(base[:, None, None] * E[:, :, None] * np.exp(lift)[None, None, :], _EPS)
    x = X[:, :, None]
    lg = special.gammaln(x + 1.0)

    def nll(lt):
        th = math.exp(lt)
        lp = (special.gammaln(x + th) - special.gammaln(th) - lg
              + th * (math.log(th) - np.log(th + mu)) + x * (np.log(mu) - np.log(th + mu)))
        return -float((Gm * lp).sum())

    lo, hi = (math.log(b) for b in _THETA_BOUNDS)
    r = optimize.minimize_scalar(nll, bounds=(lo, hi), method="bounded",
                                 options={"xatol": 1e-3})
    return float(math.exp(r.x)) if r.success else theta


def _init_base(X, E, M):
    base = np.empty(X.shape[0])
    for i in range(X.shape[0]):
        r = X[i][M[i]] / E[i][M[i]]
        med = float(np.median(r)) if r.size else 0.0
        mean = float(r.mean()) if r.size else 0.0
        base[i] = med if med > 0 else 0.5 * mean
    return np.maximum(base, _EPS)


def _bw(X, E, M, live, K, dist, *, lift, A, pi, theta, base, max_iter, tol, min_log_lift):
    allowed = _ALLOWED[K]
    ll_prev = -np.inf
    it, converged = 0, False
    for it in range(1, max_iter + 1):
        logB = _log_emis(X, E, M, base, lift, dist, theta)
        _, G, xi, ll_s = _forward_backward(logB, A, pi, live)
        ll = float(ll_s.sum())
        if abs(ll - ll_prev) <= tol * max(1.0, abs(ll)):
            converged = True
            break
        ll_prev = ll
        # initial distribution
        pi = np.maximum(G[:, 0].mean(0), _TRANS_FLOOR)
        pi /= pi.sum()
        # transitions, structural zeros preserved
        xi = np.where(allowed, np.maximum(xi, 0.0), 0.0)
        rows = xi.sum(1, keepdims=True)
        A_new = np.where(rows > 0, xi / np.maximum(rows, _EPS), A)
        A_new = np.where(allowed, np.maximum(A_new, _TRANS_FLOOR), 0.0)
        A = A_new / A_new.sum(1, keepdims=True)
        base, lift = _m_step_rates(X, E, M, G, base, lift, dist, theta, min_log_lift)
        if dist == "negbinom":
            theta = _m_step_theta(X, E, M, G, base, lift, theta)
    # final likelihood at the returned parameters
    logB = _log_emis(X, E, M, base, lift, dist, theta)
    _, G, _, ll_s = _forward_backward(logB, A, pi, live)
    return dict(lift=lift, A=A, pi=pi, theta=theta, base=base, ll=float(ll_s.sum()),
                G=G, n_iter=it, converged=converged)


def _pearson_dispersion(X, E, M, base, lift, G) -> float:
    mu = np.maximum(base[:, None, None] * E[:, :, None] * np.exp(lift)[None, None, :], _EPS)
    r2 = (X[:, :, None] - mu) ** 2 / mu
    n = M.sum()
    return float((G * r2 * M[:, :, None]).sum() / n) if n else float("nan")


def _degenerate_params(K, seed, min_log_lift, n_series_total, note_key="", train_days=None):
    return HMMParams(
        n_states=K, dist="poisson", lift=tuple([0.0] * K), theta=float("nan"),
        trans=tuple(tuple(float(v) for v in r) for r in _INIT_TRANS[K]),
        init=tuple([1.0 / K] * K), loglik=float("nan"), n_obs=0, n_series=0, n_iter=0,
        converged=False, seed=seed, dispersion=float("nan"), min_log_lift=min_log_lift,
        train_days=train_days, series_base=tuple([float("nan")] * n_series_total),
        pool_key=note_key, notes="degenerate",
    )


def fit_pooled(
    series_list: Sequence[Sequence[float]],
    exposure_list: Optional[Sequence[Optional[Sequence[float]]]] = None,
    n_states: int = 2,
    seed: int = HMM_SEED,
    max_iter: int = 200,
    *,
    dist: str = "auto",
    tol: float = 1e-7,
    n_init: int = 2,
    min_log_lift: float = math.log(2.0),
    pool_key: str = "",
    min_days: int = MIN_DAYS,
    min_total: float = MIN_TOTAL,
    min_mean: float = MIN_MEAN,
) -> HMMParams:
    """Fit one HMM whose shape parameters are shared by all series in the pool.

    Series failing `hmm_support_ok` are excluded (their ``series_base`` is NaN).
    ``dist='auto'`` fits Poisson, then -- only if the posterior Pearson dispersion
    exceeds 1.5 -- also NB, and keeps whichever has the lower BIC. ``n_init``
    restarts: the first from the fixed default start, the rest jittered by ``seed``;
    the highest likelihood wins. Deterministic given ``seed``.
    """
    if n_states not in _ALLOWED:
        raise ValueError("n_states must be 2 or 3")
    if dist not in ("auto", "poisson", "negbinom"):
        raise ValueError("dist must be auto|poisson|negbinom")
    K = n_states
    keep = [i for i, x in enumerate(series_list)
            if hmm_support_ok(x, min_days=min_days, min_total=min_total, min_mean=min_mean)[0]]
    if not keep:
        return _degenerate_params(K, seed, min_log_lift, len(series_list), pool_key)
    X, E, M, live, _ = _pack([series_list[i] for i in keep],
                             None if exposure_list is None else [exposure_list[i] for i in keep])
    rng = np.random.default_rng(seed)
    base0 = _init_base(X, E, M)

    def run(d, starts):
        best = None
        for lift0, A0, pi0, th0, b0 in starts:
            r = _bw(X, E, M, live, K, d, lift=lift0.copy(), A=A0.copy(), pi=pi0.copy(),
                    theta=th0, base=b0.copy(), max_iter=max_iter, tol=tol,
                    min_log_lift=min_log_lift)
            if best is None or r["ll"] > best["ll"] + 1e-9:
                best = r
        return best

    starts = []
    for j in range(max(1, n_init)):
        lift0 = _INIT_LIFT[K].copy()
        A0 = _INIT_TRANS[K].copy()
        if j > 0:  # seeded jitter of the start, structure kept
            lift0[1:] = np.maximum(lift0[1:] + rng.normal(0, 0.4, K - 1), min_log_lift)
            A0 = np.where(_ALLOWED[K], A0 * np.exp(rng.normal(0, 0.5, A0.shape)), 0.0)
            A0 /= A0.sum(1, keepdims=True)
        pi0 = np.full(K, 0.1 / max(K - 1, 1))
        pi0[0] = 0.9
        starts.append((lift0, A0, pi0, float("nan"), base0))
    best = run("poisson", starts)
    disp = _pearson_dispersion(X, E, M, best["base"], best["lift"], best["G"])
    n_obs = int(M.sum())

    def as_params(r, d):
        base_full = [float("nan")] * len(series_list)
        for j, i in enumerate(keep):
            base_full[i] = float(r["base"][j])
        return HMMParams(
            n_states=K, dist=d, lift=tuple(float(v) for v in r["lift"]),
            theta=float(r["theta"]) if d == "negbinom" else float("nan"),
            trans=tuple(tuple(float(v) for v in row) for row in r["A"]),
            init=tuple(float(v) for v in r["pi"]), loglik=float(r["ll"]), n_obs=n_obs,
            n_series=len(keep), n_iter=int(r["n_iter"]), converged=bool(r["converged"]),
            seed=int(seed), dispersion=disp, min_log_lift=float(min_log_lift),
            series_base=tuple(base_full), pool_key=pool_key,
        )

    p_pois = as_params(best, "poisson")
    want_nb = dist == "negbinom" or (dist == "auto" and np.isfinite(disp)
                                     and disp > _DISPERSION_TRIGGER)
    if not want_nb:
        return p_pois
    # NB starts from the Poisson solution with a method-of-moments-ish theta
    th0 = float(np.clip(1.0 / max(disp - 1.0, 1e-3) * max(float(np.mean(best["base"])), 0.1),
                        *_THETA_BOUNDS))
    nb = run("negbinom", [(best["lift"], best["A"], best["pi"], th0, best["base"])])
    p_nb = as_params(nb, "negbinom")
    if dist == "negbinom" or bic(p_nb) < bic(p_pois):
        return p_nb
    return p_pois


def fit_on_prefix(
    series_list: Sequence[Sequence[float]],
    exposure_list: Optional[Sequence[Optional[Sequence[float]]]],
    train_days: int,
    **kw,
) -> HMMParams:
    """`fit_pooled` on ``x[:train_days]`` of every series -> frozen params + bases.

    Use the result (and ``params.series_base[i]``) for causal scoring of days at
    and after ``train_days``: nothing past the cut-off influences any parameter.
    ``kw`` may include ``n_states='auto'`` to run `select_states` on the prefix.
    """
    xs = [np.asarray(x, dtype=float)[:train_days] for x in series_list]
    es = None if exposure_list is None else [
        None if e is None else np.asarray(e, dtype=float)[:train_days] for e in exposure_list]
    if kw.get("n_states") == "auto":
        kw = {k: v for k, v in kw.items() if k != "n_states"}
        p, _ = select_states(xs, es, **kw)
    else:
        p = fit_pooled(xs, es, **kw)
    return replace(p, train_days=int(train_days))


def select_states(
    series_list: Sequence[Sequence[float]],
    exposure_list: Optional[Sequence[Optional[Sequence[float]]]] = None,
    *,
    margin: float = 10.0,
    **kw,
) -> tuple[HMMParams, dict]:
    """Fit 2 and 3 states; keep 3 only if its BIC beats 2's by more than ``margin``."""
    kw.pop("n_states", None)
    p2 = fit_pooled(series_list, exposure_list, n_states=2, **kw)
    p3 = fit_pooled(series_list, exposure_list, n_states=3, **kw)
    b2, b3 = bic(p2), bic(p3)
    chosen = p3 if (np.isfinite(b3) and b3 < b2 - margin) else p2
    return chosen, {"bic_2": b2, "bic_3": b3, "margin": margin, "chosen": chosen.n_states}


def fit_series_base(
    counts: Sequence[float],
    exposure: Optional[Sequence[float]],
    params: HMMParams,
    *,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> float:
    """Per-series quiet rate with the pooled shape frozen (EM over ``base`` only).

    Fit it on the same training prefix as the params if it will feed causal scoring.
    """
    x = np.asarray(counts, dtype=float)
    X, E, M, live, _ = _pack([x], None if exposure is None else [exposure])
    if not M.any():
        return float("nan")
    base = _init_base(X, E, M)
    lift, A, pi = (np.asarray(params.lift), np.asarray(params.trans), np.asarray(params.init))
    for _ in range(max_iter):
        logB = _log_emis(X, E, M, base, lift, params.dist, params.theta)
        _, G, _, _ = _forward_backward(logB, A, pi, live)
        new, _ = _m_step_rates(X, E, M, G, base, lift, params.dist, params.theta,
                               params.min_log_lift)
        done = abs(new[0] - base[0]) <= tol * max(base[0], _EPS)
        base = new
        if done:
            break
    return float(base[0])


# --------------------------------------------------------------------------- #
# Posteriors
# --------------------------------------------------------------------------- #
def _posteriors(counts, exposure, params, base, causal):
    if not params.ok:
        raise ValueError("params are degenerate")
    x = np.asarray(counts, dtype=float)
    if base is None:
        if causal:
            raise ValueError(
                "causal scoring needs a frozen base (params.series_base[i] from "
                "fit_on_prefix, or fit_series_base on the training prefix); "
                "estimating it from this series would look ahead")
        base = fit_series_base(x, exposure, params)
    if not (np.isfinite(base) and base > 0):
        raise ValueError("base must be a positive finite rate")
    X, E, M, live, _ = _pack([x], None if exposure is None else [exposure])
    logB = _log_emis(X, E, M, np.array([float(base)]), np.asarray(params.lift),
                     params.dist, params.theta)
    alpha, gamma, _, _ = _forward_backward(
        logB, np.asarray(params.trans), np.asarray(params.init), live, backward=not causal)
    return (alpha if causal else gamma)[0]


def smoothed_posteriors(
    counts: Sequence[float],
    exposure: Optional[Sequence[float]],
    params: HMMParams,
    *,
    base: Optional[float] = None,
) -> np.ndarray:
    """(n, K) p(z_t | x_{0..n-1}) -- retrospective; uses the whole series.

    ``base=None`` estimates the series' quiet rate from the series itself, which
    is fine retrospectively (the posteriors already see the future).
    """
    return _posteriors(counts, exposure, params, base, causal=False)


def filtered_posteriors(
    counts: Sequence[float],
    exposure: Optional[Sequence[float]],
    params: HMMParams,
    *,
    base: float,
) -> np.ndarray:
    """(n, K) p(z_t | x_{0..t}) -- causal; row t never depends on x[t+1:].

    ``params`` and ``base`` must be frozen from a training window
    (`fit_on_prefix`); required here so the caller cannot leak by default.
    """
    return _posteriors(counts, exposure, params, base, causal=True)


# --------------------------------------------------------------------------- #
# BurstResult adapter
# --------------------------------------------------------------------------- #
def hmm_burst_result(
    counts: Sequence[float],
    exposure: Optional[Sequence[float]],
    params: HMMParams,
    *,
    causal: bool = False,
    threshold: float = 0.5,
    base: Optional[float] = None,
    min_days: int = MIN_DAYS,
    min_total: float = MIN_TOTAL,
    min_mean: float = MIN_MEAN,
) -> "S.BurstResult":
    """HMM posteriors -> `stats_v2.BurstResult` (feeds `extract_episodes` unchanged).

    * ``state[t] = 1`` iff P(active or decay at t) > ``threshold``.
    * ``intensity[t]`` = that burst posterior, on every day (unlike Kleinberg's LLR
      weight it is bounded in [0, 1]; episode ``level`` = peak posterior).
    * ``lambda0`` = base * mean exposure (quiet expected count/day);
      ``lambda1`` = lambda0 * exp(lift[active]).
    * Below support, with degenerate params, or with an unusable base, returns
      ``notes == "degenerate"`` exactly like `kleinberg_2state`, so a
      `detect_bursts`-style caller falls back to CUSUM. The state is all-zero (it
      must stay an int array for `extract_episodes`) but the intensity is NaN, not
      0: the HMM made no statement about those days.
    """
    x = np.asarray(counts, dtype=float)
    n = x.size
    e = _as_exposure(exposure, n)
    method = "hmm_v1_filtered" if causal else "hmm_v1"
    obs = x[np.isfinite(x)]
    lam0_raw = float(obs.mean()) if obs.size else float("nan")

    def degenerate():
        return S.BurstResult(method, np.zeros(n, dtype=int), np.full(n, np.nan),
                             lam0_raw, float("nan"), "degenerate")

    ok, _ = hmm_support_ok(x, min_days=min_days, min_total=min_total, min_mean=min_mean)
    if not ok or not params.ok:
        return degenerate()
    if base is not None and not (np.isfinite(base) and base > 0):
        return degenerate()
    if base is None and not causal:
        base = fit_series_base(x, e, params)  # retrospective only; causal raises below
    post = _posteriors(x, e, params, base, causal)
    p_burst = post[:, 1:].sum(1)
    state = (p_burst > threshold).astype(int)
    ef = e[np.isfinite(e) & (e > 0)]
    lam0 = float(base * ef.mean()) if ef.size else float("nan")
    lam1 = lam0 * math.exp(params.lift[1])
    notes = (f"K={params.n_states} dist={params.dist} "
             f"{'filtered' if causal else 'smoothed'} thr={threshold:g}")
    return S.BurstResult(method, state, p_burst, lam0, lam1, notes)


def detect_bursts_hmm(
    counts: Sequence[float],
    exposure: Optional[Sequence[float]],
    params: HMMParams,
    *,
    base: Optional[float] = None,
    causal: bool = False,
    threshold: float = 0.5,
    baseline_mu: Optional[Sequence[float]] = None,
    min_days: int = MIN_DAYS,
    min_total: float = MIN_TOTAL,
    min_mean: float = MIN_MEAN,
    s: float = 2.0,
    cusum_h: float = 5.0,
) -> "S.BurstResult":
    """Drop-in analogue of `stats_v2.detect_bursts`: HMM when supported, else CUSUM."""
    res = hmm_burst_result(counts, exposure, params, causal=causal, threshold=threshold,
                           base=base, min_days=min_days, min_total=min_total,
                           min_mean=min_mean)
    if res.notes != "degenerate":
        return res
    _, why = hmm_support_ok(counts, min_days=min_days, min_total=min_total, min_mean=min_mean)
    res = S.poisson_cusum(counts, baseline_mu=baseline_mu, s=s, h=cusum_h)
    res.notes = f"{why} below HMM support or params degenerate"
    return res


# --------------------------------------------------------------------------- #
# Simulation helper + self-checks
# --------------------------------------------------------------------------- #
def _simulate(rng, n, base, lift, exposure=None, episodes=(), dist="poisson", theta=5.0):
    """Counts with burst days = union of ``episodes`` [(start, end_inclusive)]."""
    e = np.ones(n) if exposure is None else np.asarray(exposure, dtype=float)
    z = np.zeros(n, dtype=int)
    for a, b in episodes:
        z[a : b + 1] = 1
    mu = base * e * np.exp(np.where(z == 1, lift, 0.0))
    if dist == "poisson":
        return rng.poisson(mu).astype(float), z
    return rng.negative_binomial(theta, theta / (theta + mu)).astype(float), z


def run_self_checks(*, verbose: bool = False) -> dict:
    """Unit-test-style assertions. Returns {check_name: bool}; all must be True."""
    res: dict[str, bool] = {}
    rng = np.random.default_rng(HMM_SEED)
    n = 120
    eps_true = [[(30, 37)], [(60, 70)], [(15, 20), (90, 98)], [(45, 52)], [(100, 108)], [(70, 76)]]
    xs, zs = [], []
    for ep in eps_true:
        x, z = _simulate(rng, n, 5.0, math.log(4.0), episodes=ep)
        xs.append(x)
        zs.append(z)
    p = fit_pooled(xs, None, n_states=2, seed=HMM_SEED, max_iter=150)
    res["fit_converged"] = bool(p.ok and p.converged)
    res["fit_recovers_lift"] = bool(abs(p.lift[1] - math.log(4.0)) < 0.3)
    res["fit_poisson_on_poisson_data"] = bool(p.dist == "poisson")

    # 1. posteriors are distributions
    g = smoothed_posteriors(xs[0], None, p)
    f = filtered_posteriors(xs[0], None, p, base=p.series_base[0])
    res["smoothed_sum_to_one"] = bool(np.allclose(g.sum(1), 1.0))
    res["filtered_sum_to_one"] = bool(np.allclose(f.sum(1), 1.0))

    # 2. recovery of simulated known states
    ious = []
    for x, z in zip(xs, zs):
        st = hmm_burst_result(x, None, p).state
        inter = int(((st == 1) & (z == 1)).sum())
        union = int(((st == 1) | (z == 1)).sum())
        ious.append(inter / union if union else 1.0)
    res["recovers_known_bursts"] = bool(min(ious) > 0.6)

    # 3. no-burst series -> no episodes
    quiet = [rng.poisson(5.0, n).astype(float) for _ in range(4)]
    n_ep = 0
    for q in quiet:
        br = hmm_burst_result(q, None, p)
        n_ep += len(S.extract_episodes(br.state, q, br.intensity))
    res["quiet_series_no_episodes"] = bool(n_ep == 0)

    # 4. exposure offset: an ingestion surge is not a burst
    e = np.ones(n)
    e[50:65] = 3.0
    surge = [_simulate(rng, n, 5.0, 0.0, exposure=e)[0] for _ in range(4)]
    ps = fit_pooled(surge, [e] * 4, n_states=2, seed=HMM_SEED)
    flagged = sum(int(hmm_burst_result(x, e, ps).state[50:65].sum()) for x in surge)
    res["exposure_surge_not_burst"] = bool(flagged <= 2)

    # 5. no look-ahead: filtered(t) == smoothed(x[:t+1])[-1], future mutation inert
    pf = fit_on_prefix(xs, None, 60, n_states=2, seed=HMM_SEED)
    b0 = pf.series_base[1]
    full = filtered_posteriors(xs[1], None, pf, base=b0)
    ok = all(np.allclose(full[t], smoothed_posteriors(xs[1][: t + 1], None, pf, base=b0)[-1])
             for t in (20, 61, 65, 90))
    res["filtered_equals_truncated_smoothed"] = bool(ok)
    mut = xs[1].copy()
    mut[75:] = 500.0
    res["filtered_no_future_leakage"] = bool(
        np.allclose(full[:75], filtered_posteriors(mut, None, pf, base=b0)[:75]))
    try:
        filtered_posteriors(xs[1], None, pf, base=None)  # type: ignore[arg-type]
        res["causal_requires_frozen_base"] = False
    except ValueError:
        res["causal_requires_frozen_base"] = True

    # 6. support gate mirrors stats_v2 (degenerate, NULL intensity)
    short = hmm_burst_result(np.array([0, 1, 0, 2, 0, 1, 0.0]), None, p)
    res["short_series_degenerate"] = bool(
        short.notes == "degenerate" and short.state.sum() == 0 and np.isnan(short.intensity).all())
    res["detect_routes_short_to_cusum"] = bool(
        detect_bursts_hmm(np.array([0, 1, 0, 2, 0, 1, 0.0]), None, p).method == "poisson_cusum")

    # 7. no underflow on a long, high-count series
    long_x, _ = _simulate(rng, 5000, 200.0, math.log(3.0), episodes=[(1000, 1100), (4000, 4040)])
    gl = smoothed_posteriors(long_x, None, p)
    res["long_series_no_underflow"] = bool(np.isfinite(gl).all() and np.allclose(gl.sum(1), 1.0))

    # 8. serialisation round-trip and determinism
    res["params_roundtrip"] = bool(HMMParams.from_json(p.to_json()).to_json() == p.to_json())
    res["deterministic_given_seed"] = bool(
        fit_pooled(xs, None, n_states=2, seed=HMM_SEED, max_iter=150).to_json() == p.to_json())

    # 9. 3-state structure keeps its structural zeros
    p3 = fit_pooled(xs, None, n_states=3, seed=HMM_SEED, max_iter=150)
    A3 = np.asarray(p3.trans)
    res["three_state_structural_zeros"] = bool(A3[0, 2] == 0.0 and A3[1, 0] == 0.0)

    if verbose:
        for k, v in res.items():
            print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return res


if __name__ == "__main__":
    out = run_self_checks(verbose=True)
    raise SystemExit(0 if all(out.values()) else 1)
