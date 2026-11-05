#!/usr/bin/env python3
"""Offline tests for `lib/event_chains.py` (synthetic data only; no DuckDB, no Postgres).

Run:  .venv/bin/python scripts/tests/test_event_chains.py
  or: .venv/bin/python -m pytest scripts/tests/test_event_chains.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import event_chains as EC  # noqa: E402


class Scn:
    """Tiny synthetic corpus builder: episodes plus the posts that make them up."""

    def __init__(self):
        self.eps, self.rows, self.nb, self.parents, self.cen = [], [], {}, {}, {}
        self._n = 0

    def episode(self, topic, start, end, n_hard=10, n_auth=5, platform="x", author_prefix="a",
               posts_per_author=None):
        self.eps.append({"episode_id": f"{topic}_{platform}__{start}", "topic_id": topic,
                         "platform": platform, "start_day": start, "end_day": end})
        for k in range(n_hard):
            self._n += 1
            au = f"{author_prefix}{topic}{k % n_auth}"
            self.rows.append(dict(post_uid=f"p{self._n}", author_uid=au, topic_id=topic,
                                  platform=platform, w=1.0, day=start + k % (end - start + 1)))

    def shared(self, ta, tb, day, n, wa=0.5, wb=0.5, n_auth=None, platform="x", author="s"):
        for k in range(n):
            self._n += 1
            au = f"{author}{k % n_auth}" if n_auth else f"{author}{k}"
            for t, w in ((ta, wa), (tb, wb)):
                self.rows.append(dict(post_uid=f"p{self._n}", author_uid=au, topic_id=t,
                                      platform=platform, w=w, day=day))

    def neighbours(self, a, b):
        self.nb.setdefault(a, []).append(b)
        self.nb.setdefault(b, []).append(a)

    def frames(self):
        return pd.DataFrame(self.eps), pd.DataFrame(self.rows)

    def chains(self, **kw):
        e, m = self.frames()
        return EC.build_event_chains(e, m, self.nb, self.parents, self.cen, **kw)


def _members(chains):
    return {frozenset(g["episode_id"]) for _, g in chains.groupby("chain_id")}


def _linked_pair():
    s = Scn()
    s.episode("t1", 10, 12)
    s.episode("t2", 11, 13)
    s.neighbours("t1", "t2")
    s.shared("t1", "t2", 11, 8)          # 8 boundary posts, 8 authors, w 0.5 each
    return s


def test_related_episodes_link():
    links, chains = _linked_pair().chains()
    assert len(links) == 1 and len(chains) == 2
    r = links.iloc[0]
    assert (r.src_episode, r.dst_episode) == ("t1_x__10", "t2_x__11"), "directed by start day"
    assert r.lag_days == 1 and r.route == "overlap" and r.topic_relation == "neighbour"
    assert abs(r.shared_weight - 4.0) < 1e-9 and r.overlap_ratio >= EC.THETA
    assert list(chains.sort_values("order").episode_id) == ["t1_x__10", "t2_x__11"]
    assert set(chains.columns) == {"chain_id", "episode_id", "order", "link_score",
                                   "n_topics", "n_platforms", "n_authors"}
    assert (chains.n_topics == 2).all() and (chains.n_platforms == 1).all()


def test_unrelated_episodes_do_not_link():
    # not neighbours / no parent: shared posts alone are not enough
    s = _linked_pair()
    s.nb.clear()
    assert s.chains()[0].empty
    # related and sharing posts, but too far apart in time
    s = Scn()
    s.episode("t1", 10, 12)
    s.episode("t2", 30, 32)
    s.neighbours("t1", "t2")
    s.shared("t1", "t2", 11, 8)          # shared posts only exist in t1's window...
    assert s.chains()[0].empty           # ...so t2's window never holds them: no evidence
    # related, in time, but no shared posts and no centroid route
    s = Scn()
    s.episode("t1", 10, 12)
    s.episode("t2", 11, 13)
    s.neighbours("t1", "t2")
    assert s.chains()[0].empty
    # centroid route fires only above THETA_COS
    s.cen = {"t1": [1, 0], "t2": [0.6, 0.8]}           # cos 0.6
    assert s.chains()[0].empty
    s.cen = {"t1": [1, 0], "t2": [0.95, 0.31]}         # cos ~0.95
    links, _ = s.chains()
    assert len(links) == 1 and links.iloc[0].route == "cosine"


def test_lag_gate():
    def n_links(lag):
        s = Scn()
        s.episode("t1", 10, 10)
        s.episode("t2", 10 + lag, 10 + lag + 1)
        s.neighbours("t1", "t2")
        s.cen = {"t1": [1, 0], "t2": [1, 0]}
        return len(s.chains()[0])

    assert n_links(EC.L_DAYS) == 1
    assert n_links(EC.L_DAYS + 1) == 0


def test_single_author_rejected():
    # shared-post route: 8 shared posts but one account posted them all
    s = Scn()
    s.episode("t1", 10, 12)
    s.episode("t2", 11, 13)
    s.neighbours("t1", "t2")
    s.shared("t1", "t2", 11, 8, wa=1.0, wb=1.0, n_auth=1)
    links, _ = s.chains()
    assert links.empty, "one author posting 8x must not form a link"
    # control: the same shape with 4 distinct authors links
    s2 = Scn()
    s2.episode("t1", 10, 12)
    s2.episode("t2", 11, 13)
    s2.neighbours("t1", "t2")
    s2.shared("t1", "t2", 11, 8, wa=1.0, wb=1.0, n_auth=4)
    assert len(s2.chains()[0]) == 1
    # centroid route: each episode is one author posting 40x
    s3 = Scn()
    s3.episode("t1", 10, 12, n_hard=40, n_auth=1)
    s3.episode("t2", 11, 13, n_hard=40, n_auth=1)
    s3.neighbours("t1", "t2")
    s3.cen = {"t1": [1, 0], "t2": [1, 0]}
    assert s3.chains()[0].empty
    s3 = Scn()
    s3.episode("t1", 10, 12, n_hard=40, n_auth=5)
    s3.episode("t2", 11, 13, n_hard=40, n_auth=5)
    s3.neighbours("t1", "t2")
    s3.cen = {"t1": [1, 0], "t2": [1, 0]}
    assert len(s3.chains()[0]) == 1


def _two_chains(bridge_w):
    """Chain X (tx1->tx2) and chain Y (ty1->ty2), plus ONE post bridging tx2 and ty1."""
    s = Scn()
    s.episode("tx1", 10, 12, n_hard=12)
    s.episode("tx2", 12, 14, n_hard=12)
    s.episode("ty1", 13, 15, n_hard=12)
    s.episode("ty2", 14, 16, n_hard=12)
    for a, b in (("tx1", "tx2"), ("ty1", "ty2"), ("tx2", "ty1")):
        s.neighbours(a, b)
    s.shared("tx1", "tx2", 12, 8)
    s.shared("ty1", "ty2", 14, 8)
    s.shared("tx2", "ty1", 14, 1, wa=bridge_w, wb=bridge_w, author="bridge")
    return s


def test_one_ambiguous_post_cannot_merge_chains():
    # default gates: two chains, whatever the bridge weight (one post = one author < K)
    for w in (0.5, 1.0):
        assert len(_members(_two_chains(w).chains()[1])) == 2, w
    # relax the author gate and theta so only the *weight* stands between chains: a w=0.5
    # post adds 0.5 to the overlap and stays below theta; counted as 1 it would fuse them
    relaxed = dict(theta=0.05, k_authors=0.5)
    assert len(_members(_two_chains(0.5).chains(**relaxed)[1])) == 2, "w=0.5 fused chains"
    fused = _members(_two_chains(1.0).chains(**relaxed)[1])
    assert len(fused) == 1 and len(next(iter(fused))) == 4, "control: w=1 should fuse"
    links = _two_chains(0.5).chains(**relaxed)[0]
    assert not ((links.src_episode == "tx2_x__12") & (links.dst_episode == "ty1_x__13")).any()


def test_chain_ids_stable_under_reorder():
    s = _two_chains(0.5)
    s.cen = {"tx1": [1, 0], "tx2": [1, 0]}
    e, m = s.frames()
    base = EC.build_event_chains(e, m, s.nb, s.parents, s.cen)
    rng = np.random.default_rng(3)
    for _ in range(5):
        e2 = e.sample(frac=1.0, random_state=int(rng.integers(1 << 30))).reset_index(drop=True)
        m2 = m.sample(frac=1.0, random_state=int(rng.integers(1 << 30))).reset_index(drop=True)
        nb2 = {k: list(reversed(v)) for k, v in reversed(list(s.nb.items()))}
        got = EC.build_event_chains(e2, m2, nb2, s.parents, s.cen)
        pd.testing.assert_frame_equal(base[1], got[1])
        pd.testing.assert_frame_equal(base[0], got[0])
    ids = set(base[1].chain_id)
    assert all(i.startswith("chain_") for i in ids)
    assert EC.chain_id_of(["b", "a"]) == EC.chain_id_of(["a", "b"])


def _background(s, rng, n_topics=6, span=150):
    """Random single episodes on loosely related topics (same parent, near-identical
    centroids), no shared posts: links can only arise from chance time coincidence."""
    for k in range(n_topics):
        t = f"r{k}"
        st = int(rng.integers(0, span))
        s.episode(t, st, st + int(rng.integers(0, 3)), n_hard=12, n_auth=6)
        s.parents[t] = "P"
        s.cen[t] = [1.0, 0.01 * k]


def _planted(n_ep=8):
    s = Scn()
    for k in range(n_ep):                       # one episode per day, consecutive topics
        s.episode(f"c{k}", 60 + k, 60 + k + 1, n_hard=10)
    for k in range(n_ep - 1):
        s.neighbours(f"c{k}", f"c{k + 1}")
        s.shared(f"c{k}", f"c{k + 1}", 60 + k + 1, 8)
    return s


def test_circular_shift_null():
    # planted chain (7 links) on top of a little random background
    s = _planted()
    _background(s, np.random.default_rng(0))
    e, m = s.frames()
    # pin the observed range so the null shifts over a realistic 150-day window
    s.eps.append({"episode_id": "pad_x__0", "topic_id": "pad", "platform": "x",
                  "start_day": 0, "end_day": 0})
    s.eps.append({"episode_id": "pad_x__149", "topic_id": "pad", "platform": "pp",
                  "start_day": 149, "end_day": 149})
    e, m = s.frames()
    res = EC.circular_shift_null(e, m, n_perm=300, seed=1, neighbours=s.nb, parents=s.parents,
                                 centroids=s.cen)
    assert res["observed"] >= 7 and res["exceeds_p95"], res["observed"]
    assert res["p_value"] < 0.05, res["p_value"]
    assert res["n_perm"] == 300 and len(res["null"]) == 300

    # same seed => same answer
    again = EC.circular_shift_null(e, m, n_perm=300, seed=1, neighbours=s.nb,
                                   parents=s.parents, centroids=s.cen)
    assert again["p_value"] == res["p_value"] and (again["null"] == res["null"]).all()

    # random episodes: links are chance coincidences, so p must not be small
    pvals = []
    for seed in range(12):
        r = Scn()
        _background(r, np.random.default_rng(100 + seed), n_topics=10)
        r.eps.append({"episode_id": "pad_x__0", "topic_id": "pad", "platform": "x",
                      "start_day": 0, "end_day": 0})
        r.eps.append({"episode_id": "pad_x__149", "topic_id": "pad", "platform": "pp",
                      "start_day": 149, "end_day": 149})
        e, m = r.frames()
        pvals.append(EC.circular_shift_null(e, m, n_perm=300, seed=seed, neighbours=r.nb,
                                            parents=r.parents, centroids=r.cen)["p_value"])
    assert pvals[0] > 0.2, pvals
    assert float(np.median(pvals)) > 0.2, pvals
    assert sum(p < 0.05 for p in pvals) <= 2, pvals          # ~5% expected under the null


def test_empty_inputs():
    e = pd.DataFrame({"episode_id": ["a_x__1"], "topic_id": ["a"], "platform": ["x"],
                      "start_day": [1], "end_day": [2]})
    m = pd.DataFrame(columns=["post_uid", "author_uid", "topic_id", "platform", "w", "day"])
    links, chains = EC.build_event_chains(e, m)
    assert links.empty and chains.empty
    r = EC.circular_shift_null(e, m, n_perm=5)
    assert r["observed"] == 0 and r["p_value"] == 1.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
