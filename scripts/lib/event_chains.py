"""Event chains: link burst episodes that are the same story moving through time.

Pure functions only (no DuckDB, no Postgres), so everything is unit-testable on synthetic
frames. This is Step 3 of the soft-topic / event-chain plan. Inputs are

  episodes    one row per burst episode, as built in `scripts/07_metrics_v2.py`
              (`episode_id` = `{topic}_{platform}__{start_day}`): needs `episode_id`,
              `topic_id`, `start_day`; uses `end_day` and `platform` when present.
  membership  long frame of post membership: `post_uid`, `author_uid`, `topic_id`, `w`
              (soft weight, 1.0 for an unambiguous post), `day`; `platform` is used to
              match an episode's series when present. Extra columns (e.g. an embedding
              row index) are ignored.
  neighbours  {topic_id: [top-2 neighbour topic_ids]} (from centroid similarity)
  parents     {topic_id: parent_id} (v4 hierarchy)
  centroids   {topic_id: vector} for the centroid-cosine route (optional; without it only
              the shared-post route can fire)

Link rule. Episodes A and B (A is the one with the earlier start day) are linked iff ALL:
  1. time:      start lag <= L_DAYS, and the windows overlap or the gap is <= L_DAYS
  2. topics:    same topic (other platform), mutual top-2 neighbours, or a shared parent
  3. evidence:  EITHER weighted shared-post overlap >= THETA   (route "overlap")
                OR     centroid cosine            >= THETA_COS (route "cosine")
  4. authors:   the route's evidence rests on >= K_AUTHORS effective distinct authors

"Effective" authors are weighted: an author counts min(1, sum of their post weights), so an
ambiguous post (w = 0.5) is only half an author and a single account posting 40 times is
still one. Ambiguous posts contribute their *weight*, never 1, to the overlap, so one
boundary post cannot fuse two chains.

Chains are connected components (union-find) over accepted links. Singletons are not
chains. `circular_shift_null` is plan noise gate 2.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

__all__ = [
    "L_DAYS", "THETA", "THETA_COS", "K_AUTHORS",
    "build_event_chains", "circular_shift_null", "chain_id_of",
]

# ---------------------------------------------------------------------------
# Constants. PROVISIONAL: tune on the FIRST HALF of the window only, evaluate on the second
# (plan "Tuning leakage"). Do not retune after looking at second-half results.
# ---------------------------------------------------------------------------

# Max start-day lag (and max gap between one episode ending and the next starting).
# News propagates across platforms within days; a week-long lag links unrelated stories.
L_DAYS = 3
# Weighted shared-post overlap: sum_shared min(wA, wB) / min(sizeA, sizeB), size = sum of w.
# 0.2 = a fifth of the smaller episode must be the same posts; below that the overlap is
# the incidental boundary-post kind that soft membership creates.
THETA = 0.2
# Centroid cosine for the no-shared-post route. Topic centroids in 1024-d embedding space
# are high-cosine even for loosely related topics, so this is deliberately strict.
THETA_COS = 0.85
# Distinct (effective) authors behind a link; stops one account posting 40x creating a chain.
K_AUTHORS = 3
# Same topic on another platform counts as topically related (cross-platform propagation).
# Same (topic, platform) series are never linked: extract_episodes already separates them.
SAME_TOPIC_RELATED = True
# Only the first N neighbours of each topic are used ("top-2 neighbours").
N_NEIGHBOURS = 2

_PAIR_COLS = ["src_episode", "dst_episode", "lag_days", "overlap_ratio", "shared_weight",
              "shared_authors", "centroid_cos", "topic_relation", "route", "link_score"]


def chain_id_of(episode_ids: Iterable[str]) -> str:
    """Deterministic id: hash of the sorted member episode ids (order/row independent)."""
    key = "\x1f".join(sorted(str(e) for e in episode_ids))
    return "chain_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _day_num(s: pd.Series) -> np.ndarray:
    """Integer day ordinal from dates, timestamps, ISO strings or already-numeric days."""
    if pd.api.types.is_numeric_dtype(s):
        return s.to_numpy(dtype=np.int64)
    return pd.to_datetime(s).to_numpy().astype("datetime64[D]").astype(np.int64)


def _eff_authors(author_w: Mapping[str, float]) -> float:
    return float(sum(min(1.0, v) for v in author_w.values()))


def _topic_relation(a, b, nb: Mapping, parents: Mapping) -> Optional[str]:
    if a == b:
        return "same" if SAME_TOPIC_RELATED else None
    if b in list(nb.get(a, ()))[:N_NEIGHBOURS] and a in list(nb.get(b, ()))[:N_NEIGHBOURS]:
        return "neighbour"
    pa, pb = parents.get(a), parents.get(b)
    if pa is not None and pb is not None and pa == pb and not pd.isna(pa):
        return "parent"
    return None


def _cos(u, v) -> float:
    u, v = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    d = float(np.linalg.norm(u) * np.linalg.norm(v))
    return float(u @ v / d) if d > 0 else float("nan")


class _Prep:
    """Time-independent link evidence. Built once; the time gate is re-applied per call."""
    pass


def _prepare(episodes: pd.DataFrame, membership: pd.DataFrame, neighbours, parents,
             centroids, theta, theta_cos, k_authors) -> _Prep:
    ep = episodes.sort_values("episode_id", kind="mergesort").reset_index(drop=True)
    if ep["episode_id"].duplicated().any():
        raise ValueError("duplicate episode_id in episodes")
    n = len(ep)
    start = _day_num(ep["start_day"]) if n else np.zeros(0, dtype=np.int64)
    end = _day_num(ep["end_day"]) if "end_day" in ep and n else start.copy()
    platform = ep["platform"].astype(str).to_numpy() if "platform" in ep else np.array([""] * n)
    topic = ep["topic_id"].astype(str).to_numpy()

    nb = {str(k): [str(x) for x in v] for k, v in (neighbours or {}).items()}
    par = {str(k): v for k, v in (parents or {}).items()}
    cen = {str(k): np.asarray(v, dtype=float) for k, v in (centroids or {}).items()}

    # one row per (post, topic): a duplicated row must not double a post's weight
    m = membership.copy()
    m["topic_id"] = m["topic_id"].astype(str)
    m["_day"] = _day_num(m["day"]) if len(m) else np.zeros(0, dtype=np.int64)
    has_plat = "platform" in m.columns and "platform" in ep.columns
    if has_plat:
        m["platform"] = m["platform"].astype(str)
    keys = ["post_uid", "topic_id"] + (["platform"] if has_plat else [])
    m = (m.groupby(keys, sort=True, as_index=False)
          .agg(author_uid=("author_uid", "first"), w=("w", "max"), _day=("_day", "first")))
    groups = {k: g for k, g in m.groupby(["topic_id", "platform"] if has_plat else ["topic_id"])}

    posts = []       # per episode: {post_uid: (w, author)}
    for i in range(n):
        g = groups.get((topic[i], platform[i]) if has_plat else topic[i])
        d: dict = {}
        if g is not None:
            sel = g[(g["_day"] >= start[i]) & (g["_day"] <= end[i])]
            d = {u: (float(w), a) for u, w, a in zip(sel["post_uid"], sel["w"], sel["author_uid"])}
        posts.append(d)
    size = np.array([sum(w for w, _ in d.values()) for d in posts], dtype=float)
    ep_auth = []
    for d in posts:
        aw: dict = {}
        for w, a in d.values():
            aw[a] = aw.get(a, 0.0) + w
        ep_auth.append(aw)
    eff_ep = np.array([_eff_authors(a) for a in ep_auth], dtype=float)

    series_key = list(zip(topic, platform))
    series_ids = {s: j for j, s in enumerate(sorted(set(series_key)))}
    series = np.array([series_ids[s] for s in series_key], dtype=int)

    by_topic: dict = {}
    for i, t in enumerate(topic):
        by_topic.setdefault(t, []).append(i)
    rows = []
    topics = sorted(by_topic)
    for ia, ta in enumerate(topics):
        for tb in topics[ia:]:
            rel = _topic_relation(ta, tb, nb, par)
            if rel is None:
                continue
            cos = _cos(cen[ta], cen[tb]) if ta in cen and tb in cen else float("nan")
            for i in by_topic[ta]:
                for j in by_topic[tb]:
                    if (ta == tb and j <= i) or series[i] == series[j]:
                        continue
                    a, b = (i, j) if i < j else (j, i)      # index order == episode_id order
                    shared = posts[a].keys() & posts[b].keys()
                    sw, aw = 0.0, {}
                    for u in shared:
                        mw = min(posts[a][u][0], posts[b][u][0])
                        sw += mw
                        au = posts[a][u][1]
                        aw[au] = aw.get(au, 0.0) + mw
                    denom = min(size[a], size[b])
                    ratio = sw / denom if denom > 0 else 0.0
                    sh_auth = _eff_authors(aw)
                    ok_o = ratio >= theta and sh_auth >= k_authors
                    # Same-topic pairs share one centroid, so cosine is 1.0 by construction and
                    # is not evidence; they must link through shared posts.
                    ok_c = (np.isfinite(cos) and cos >= theta_cos and rel != "same"
                            and min(eff_ep[a], eff_ep[b]) >= k_authors)
                    if not (ok_o or ok_c):
                        continue                              # no evidence => can never link
                    score = max(ratio if ok_o else 0.0, cos if ok_c else 0.0)
                    rows.append((a, b, ratio, sw, sh_auth, cos, rel,
                                 "both" if ok_o and ok_c else ("overlap" if ok_o else "cosine"),
                                 score))
    P = _Prep()
    P.ep, P.start, P.end, P.series, P.posts = ep, start, end, series, posts
    P.n_series = len(series_ids)
    P.pairs = pd.DataFrame(rows, columns=["a", "b", "overlap_ratio", "shared_weight",
                                          "shared_authors", "centroid_cos", "topic_relation",
                                          "route", "link_score"])
    return P


def _time_ok(a: np.ndarray, b: np.ndarray, start: np.ndarray, end: np.ndarray, l_days: int):
    """Direct each pair by start day (ties: episode_id order); apply lag and gap gate."""
    fwd = start[a] <= start[b]
    src = np.where(fwd, a, b)
    dst = np.where(fwd, b, a)
    lag = start[dst] - start[src]
    gap = np.maximum(0, start[dst] - end[src])
    return src, dst, lag, (lag <= l_days) & (gap <= l_days)


def _components(n: int, edges: Iterable[tuple]) -> np.ndarray:
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)     # smaller index wins: order independent
    return np.array([find(i) for i in range(n)], dtype=int)


def build_event_chains(episodes: pd.DataFrame, membership: pd.DataFrame, neighbours=None,
                       parents=None, centroids=None, *, l_days: int = L_DAYS,
                       theta: float = THETA, theta_cos: float = THETA_COS,
                       k_authors: float = K_AUTHORS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (chain_links, event_chain).

    chain_links: src_episode, dst_episode (src starts first), lag_days, overlap_ratio,
      shared_weight, shared_authors (effective), centroid_cos, topic_relation, route,
      link_score, chain_id.
    event_chain: chain_id, episode_id, order (1-based by start day), link_score (mean of the
      chain's link scores), n_topics, n_platforms, n_authors (distinct raw authors).
    """
    P = _prepare(episodes, membership, neighbours, parents, centroids, theta, theta_cos,
                 k_authors)
    ep, pr = P.ep, P.pairs
    ids = ep["episode_id"].astype(str).to_numpy()
    empty_l = pd.DataFrame(columns=_PAIR_COLS + ["chain_id"])
    empty_c = pd.DataFrame(columns=["chain_id", "episode_id", "order", "link_score",
                                    "n_topics", "n_platforms", "n_authors"])
    if pr.empty:
        return empty_l, empty_c
    src, dst, lag, ok = _time_ok(pr["a"].to_numpy(), pr["b"].to_numpy(), P.start, P.end, l_days)
    pr = pr[ok].reset_index(drop=True)
    if pr.empty:
        return empty_l, empty_c
    src, dst, lag = src[ok], dst[ok], lag[ok]
    links = pd.DataFrame({
        "src_episode": ids[src], "dst_episode": ids[dst], "lag_days": lag.astype(int),
        **{c: pr[c] for c in ["overlap_ratio", "shared_weight", "shared_authors",
                              "centroid_cos", "topic_relation", "route", "link_score"]}})
    comp = _components(len(ep), zip(src, dst))
    links["chain_id"] = [chain_id_of(ids[comp == comp[s]]) for s in src]

    recs = []
    topic = ep["topic_id"].astype(str).to_numpy()
    plat = ep["platform"].astype(str).to_numpy() if "platform" in ep else np.array([""] * len(ep))
    mean_score = links.groupby("chain_id")["link_score"].mean()
    for root in sorted(set(comp[np.concatenate([src, dst])])):
        mem = np.flatnonzero(comp == root)
        cid = chain_id_of(ids[mem])
        mem = mem[np.lexsort((ids[mem], P.start[mem]))]
        authors = {a for i in mem for _, a in P.posts[i].values()}
        for o, i in enumerate(mem, 1):
            recs.append((cid, ids[i], o, float(mean_score[cid]), len(set(topic[mem])),
                         len(set(plat[mem])), len(authors)))
    chains = pd.DataFrame(recs, columns=empty_c.columns).sort_values(
        ["chain_id", "order"], kind="mergesort").reset_index(drop=True)
    links = links.sort_values(["chain_id", "src_episode", "dst_episode"],
                              kind="mergesort").reset_index(drop=True)
    return links, chains


def circular_shift_null(episodes: pd.DataFrame, membership: pd.DataFrame, n_perm: int = 500,
                        seed: int = 0, neighbours=None, parents=None, centroids=None, *,
                        l_days: int = L_DAYS, theta: float = THETA,
                        theta_cos: float = THETA_COS, k_authors: float = K_AUTHORS) -> dict:
    """Plan noise gate 2: do real links exceed what time-coincidence alone produces?

    Each (topic, platform) series is moved by an independent random circular shift of the
    observed day range, episodes keeping their duration; links are recounted. The post-level
    evidence (shared posts, centroid cosine, authors) is held fixed, so only the temporal
    alignment is destroyed, which is exactly what a chain claims.

    Returns {observed, null (array of n_perm counts), p95, p_value, n_perm, exceeds_p95}.
    p_value = (1 + #{null >= observed}) / (1 + n_perm), the one-sided permutation p-value.
    Caveat: shared-post evidence implies temporal overlap at the observed times, so for the
    "overlap" route the null is close to zero by construction; the gate is most informative
    for the "cosine" route, where chance temporal coincidence is the real risk.
    """
    P = _prepare(episodes, membership, neighbours, parents, centroids, theta, theta_cos,
                 k_authors)
    n_ev = len(P.pairs)
    a = P.pairs["a"].to_numpy()
    b = P.pairs["b"].to_numpy()
    obs = int(_time_ok(a, b, P.start, P.end, l_days)[3].sum()) if n_ev else 0
    null = np.zeros(int(n_perm), dtype=int)
    if n_ev and len(P.start):
        rng = np.random.default_rng(seed)
        lo, hi = int(P.start.min()), int(P.end.max())
        span = hi - lo + 1
        dur = P.end - P.start
        for p in range(int(n_perm)):
            d = rng.integers(0, span, size=P.n_series)[P.series]
            ns = lo + (P.start - lo + d) % span
            null[p] = int(_time_ok(a, b, ns, ns + dur, l_days)[3].sum())
    p95 = float(np.percentile(null, 95)) if len(null) else float("nan")
    pval = float((1 + int((null >= obs).sum())) / (1 + len(null)))
    return {"observed": obs, "null": null, "p95": p95, "p_value": pval,
            "n_perm": int(n_perm), "exceeds_p95": bool(obs > p95)}
