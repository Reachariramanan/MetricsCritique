#!/usr/bin/env python3
"""Offline tests for the plan Step 1-3 switches in scripts/07_metrics_v2.py.

Synthetic frames only: no DuckDB file is opened (an in-memory DuckDB is used for the
08 DDL check), no Postgres, nothing under data/ is written. Importing 07/08 imports
`lib.config`, which needs the repo `.env` to exist (it does not connect).

Run:  .venv/bin/python scripts/tests/test_07_switches.py
  or: .venv/bin/python -m pytest scripts/tests/test_07_switches.py
"""
import copy
import importlib.util
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M07 = _load("m07_switches_test", "07_metrics_v2.py")
M08 = _load("m08_switches_test", "08_metrics_db.py")
PARAMS0 = copy.deepcopy(M07.PARAMS)          # snapshot at import, before any test runs

LEGACY = {"topic_source": "v1", "count_mode": "hard", "burst_method": "kleinberg",
          "chain_method": "none", "soft_tau": None, "soft_m_hi": None}


# --------------------------------------------------------------------------- helpers
def _posts(n=900, n_days=40, seed=3):
    """Enriched-posts-like frame with every column aggregate_cells reads."""
    rng = np.random.default_rng(seed)
    d0 = date(2026, 6, 14)
    tone = rng.normal(0, 1, n)
    tone[rng.random(n) < 0.2] = np.nan
    hate = rng.random(n)
    hate[rng.random(n) < 0.3] = np.nan
    views = rng.integers(0, 1000, n).astype(float)
    views[rng.random(n) < 0.3] = np.nan
    return pd.DataFrame({
        "post_uid": [f"p{i:05d}" for i in range(n)],
        "platform": rng.choice(["telegram", "reddit", "x"], n),
        "topic_id": rng.choice([f"t{j}" for j in range(6)], n).astype(object),
        "day": [d0 + timedelta(days=int(k)) for k in rng.integers(0, n_days, n)],
        "author_uid": rng.choice([f"a{j}" for j in range(80)] + [None], n),
        "tone_v": tone, "hate_v": hate,
        "read_v": rng.uniform(0, 100, n), "read_ok": rng.random(n) < 0.8,
        "virality_pct_post": rng.random(n), "views": views,
        "E_sum": rng.integers(0, 50, n).astype(float),
        "reach_audience_norm_post": rng.random(n),
        "confidence": rng.random(n),
        "text_norm": rng.choice(["short", "a much longer duplicated text body here",
                                 "another long distinct body of text xyz"], n),
    })


def _ext_from_posts(posts, seed=4):
    """Synthetic post_topic_v4_ext built from the posts only (16 has not been applied)."""
    rng = np.random.default_rng(seed)
    topics = sorted(posts["topic_id"].unique())
    parent = {t: f"P{i // 2}" for i, t in enumerate(topics)}   # 3 parents x 2 topics
    t2 = [rng.choice([x for x in topics if x != t]) for t in posts["topic_id"]]
    return pd.DataFrame({
        "post_uid": posts["post_uid"], "topic_id": posts["topic_id"],
        "parent_topic_id": [parent[t] for t in posts["topic_id"]],
        "topic_id_2": t2, "parent_topic_id_2": [parent[t] for t in t2],
        "margin": rng.uniform(0.0, 0.15, len(posts)),
    })


# --------------------------------------------------------------------------- tests
def test_default_params_are_legacy():
    for k, v in LEGACY.items():
        assert PARAMS0[k] == v, f"default {k}={PARAMS0[k]!r}, legacy is {v!r}"
    args = M07.parse_args([])
    P = copy.deepcopy(PARAMS0)
    M07.apply_args(args, P)
    assert P == PARAMS0, "parse_args([]) + apply_args must not change any PARAMS value"
    assert Path(args.stage_dir) == M07.STAGE_DIR and args.tag is None
    M07.validate_params(P)                                    # defaults are valid


def test_soft_requires_v4_ext():
    P = {**PARAMS0, "count_mode": "soft"}
    try:
        M07.validate_params(P)
    except ValueError as e:
        assert "v4_ext" in str(e)
    else:
        raise AssertionError("count_mode=soft with topic_source=v1 must raise")
    # through the CLI: raises before any data is read or any directory is created
    saved = copy.deepcopy(M07.PARAMS)
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td) / "never_created"
        try:
            M07.main(["--count-mode", "soft", "--stage-dir", str(sd)])
        except ValueError as e:
            assert "v4_ext" in str(e)
        else:
            raise AssertionError("main(--count-mode soft) must raise without v4_ext")
        finally:
            M07.PARAMS.clear()
            M07.PARAMS.update(saved)
        assert not sd.exists()
    M07.validate_params({**PARAMS0, "count_mode": "soft", "topic_source": "v4_ext"})
    try:
        M07.validate_params({**PARAMS0, "count_mode": "soft", "topic_source": "v4_ext",
                             "soft_tau": 0.01})
    except ValueError:
        pass
    else:
        raise AssertionError("soft_tau without soft_m_hi must raise")


def test_weights_conserve_V_total():
    posts = _posts()
    ext = _ext_from_posts(posts)
    P = {**PARAMS0, "count_mode": "soft", "topic_source": "v4_ext"}
    ws, we = date(2026, 6, 14), date(2026, 7, 23)
    soft, info = M07.soft_setup(posts, ext, P, ws, we)
    assert info["expand"]["n_ambiguous"] > 0, "fixture must contain ambiguous posts"
    assert soft.groupby("post_uid")["w"].sum().sub(1.0).abs().max() < 1e-12
    for keys in (["topic_id", "platform", "day"], ["topic_id", "day"]):
        c = M07.aggregate_cells(soft, keys, weight_col="w")
        assert abs(c["V"].sum() - len(posts)) < 1e-9, keys
        assert c["V_primary"].sum() == len(posts), keys
        assert (c["n_authors"] <= c["V"] + 1e-9).all(), "effective authors must be <= V"
        # engagement and tone mass are split, never duplicated
        assert abs(c["views_sum"].sum() - posts["views"].sum()) < 1e-6
        assert abs(c["tone_n"].sum() - posts["tone_v"].notna().sum()) < 1e-9
    # the same frame with w == 1 everywhere reproduces the hard path on shared columns
    hard = M07.aggregate_cells(posts, ["topic_id", "platform", "day"])
    one = M07.aggregate_cells(posts.assign(w=1.0, topic_rank=1),
                              ["topic_id", "platform", "day"], weight_col="w")
    m = hard.merge(one, on=["topic_id", "platform", "day"], suffixes=("_h", "_w"))
    assert len(m) == len(hard) == len(one)
    for col in ("V", "tone_mean", "hate_mean", "hate_prevalence", "readability_mean",
                "readability_coverage", "virality_pct_raw", "reach_proxy", "n_authors",
                "author_entropy", "author_1_hhi", "duplicate_rate", "mean_confidence",
                "tone_ss", "vir_ss"):
        a, b = m[f"{col}_h"].astype(float), m[f"{col}_w"].astype(float)
        assert np.allclose(a, b, equal_nan=True, atol=1e-9), col


def test_soft_constants_use_first_half_only():
    posts = _posts(n=3000, seed=11)
    ext = _ext_from_posts(posts, seed=12)
    P = {**PARAMS0, "count_mode": "soft", "topic_source": "v4_ext"}
    ws, we = date(2026, 6, 14), date(2026, 7, 23)
    _, a = M07.soft_setup(posts, ext, P, ws, we)
    late = posts["day"] > date.fromisoformat(a["tune_days"][1])
    ext2 = ext.copy()
    ext2.loc[late.to_numpy(), "margin"] = 0.0001             # perturb the 2nd half only
    _, b = M07.soft_setup(posts, ext2, P, ws, we)
    assert (a["tau"], a["m_hi"]) == (b["tau"], b["m_hi"]), "constants leaked the 2nd half"
    assert "first half" in a["constants_source"]


def test_stage_dir_isolation():
    before = {p.name: p.stat().st_mtime_ns for p in M07.STAGE_DIR.glob("*")} \
        if M07.STAGE_DIR.exists() else None
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td) / "stage"
        args = M07.parse_args(["--stage-dir", str(sd), "--tag", "t1"])
        assert Path(args.stage_dir) == sd and args.tag == "t1"
        M07.write_stage({"burst_episodes": pd.DataFrame({"episode_id": ["e1"]}),
                         "event_chain_v1": pd.DataFrame()}, {"tag": "t1"}, sd)
        assert sorted(p.name for p in sd.iterdir()) == [
            "burst_episodes.parquet", "event_chain_v1.parquet", "run_params.json"]
    after = {p.name: p.stat().st_mtime_ns for p in M07.STAGE_DIR.glob("*")} \
        if M07.STAGE_DIR.exists() else None
    assert before == after, "default data/metrics_stage was touched"


def _panel_inputs(n_days=60, seed=5):
    rng = np.random.default_rng(seed)
    d0 = date(2026, 6, 14)
    days = [d0 + timedelta(days=i) for i in range(n_days)]
    rows = []
    for t in ("t0", "t1", "t2"):
        for pl in ("telegram", "reddit"):
            lam = np.full(n_days, 6.0)
            if t == "t1":
                lam[30:36] = 40.0                              # one planted burst
            for d, v in zip(days, rng.poisson(lam)):
                rows.append({"topic_id": t, "platform": pl, "day": d, "V": float(v)})
    cells = pd.DataFrame(rows)
    effort = pd.DataFrame([{"platform": pl, "day": d, "effort": 10.0,
                            "effort_source": "observed_active_channels"}
                           for pl in ("telegram", "reddit") for d in days])
    pdays = {pl: set(days) for pl in ("telegram", "reddit")}
    pdays["__any__"] = set(days)
    return cells, effort, pdays, d0, days[-1]


def test_hmm_switch_runs_and_tags_episodes():
    cells, effort, pdays, ws, we = _panel_inputs()
    keys = ["topic_id", "platform", "day"]
    pk, ek, dk = M07.compute_panel(cells.copy(), keys, effort, dict(PARAMS0),
                                   platform_days=pdays, window_start=ws, window_end=we)
    assert "hmm_pools" not in dk
    P = {**PARAMS0, "burst_method": "hmm"}
    ph, eh, dh = M07.compute_panel(cells.copy(), keys, effort, P,
                                   platform_days=pdays, window_start=ws, window_end=we)
    assert set(dh["hmm_pools"]) == {"telegram", "reddit"}
    for d in dh["hmm_pools"].values():
        assert d["n_states"] in (2, 3) and d["bic_2"] is not None
    assert len(eh) and set(eh["burst_method"]) == {"hmm_v1"}
    t1 = eh[eh["topic_id"] == "t1"]
    assert len(t1) == 2 and all(str(x) <= "2026-07-14" <= str(y)
                                for x, y in zip(t1["start_day"], t1["end_day"]))
    ib = ph["burst_intensity"].dropna()
    assert ((ib >= 0) & (ib <= 1)).all(), "HMM intensity is a posterior"
    # non-burst columns are untouched by the detector switch
    m = pk.merge(ph, on=keys, suffixes=("_k", "_h"))
    for c in ("V", "surprise", "surprise_q", "accel_smooth", "momentum"):
        assert np.allclose(m[f"{c}_k"].astype(float), m[f"{c}_h"].astype(float),
                           equal_nan=True), c
    ex = M07.hmm_exposure(effort, pdays, ws, we, 3.0)
    assert all(1 / 3.0 <= v <= 3.0 for v in ex.values())


def test_chains_build_and_types():
    rng = np.random.default_rng(9)
    d0 = date(2026, 6, 20)
    episodes = pd.DataFrame({
        "episode_id": ["t1_telegram__2026-06-20", "t1_reddit__2026-06-21",
                       "unassigned_x__2026-06-20"],
        "topic_id": ["t1", "t1", "unassigned"], "platform": ["telegram", "reddit", "x"],
        "start_day": [d0, d0 + timedelta(1), d0], "end_day": [d0 + timedelta(3)] * 3})
    n = 60
    members = pd.DataFrame({
        "post_uid": [f"p{i}" for i in range(n)],
        "author_uid": [f"a{i % 12}" for i in range(n)],
        "topic_id": ["t1"] * 40 + ["t2"] * 10 + ["unassigned"] * 10,
        "w": 1.0, "day": [d0 + timedelta(int(i % 4)) for i in range(n)],
        "platform": ["telegram", "reddit"] * 30})
    mat = rng.normal(size=(n, 8)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    emb = {"mat": mat, "index": {u: i for i, u in enumerate(members["post_uid"])}}
    P = {**PARAMS0, "chain_method": "v1", "chain_null_n": 50}
    links, chains, info = M07.build_chains(episodes, members, emb,
                                           pd.DataFrame({"topic_id": [], "domain": []}), P)
    assert info["n_episodes_excluded_unassigned"] == 1
    assert len(links) == 1 and links["topic_relation"].iloc[0] == "same"
    assert len(chains) == 2 and chains["chain_id"].nunique() == 1
    # 08 publishes them with explicit DDL: ids VARCHAR even when empty / all-NULL
    con = duckdb.connect()
    stage = {"event_chain_v1": chains, "chain_links_v1": links.iloc[0:0]}
    rows = M08.publish_optional_tables(con, stage)
    assert rows == {"event_chain_v1": 2, "chain_links_v1": 0}
    types = {(t, c): ty for t in rows
             for c, ty, *_ in con.execute(f'DESCRIBE "{t}"').fetchall()}
    for k in [("event_chain_v1", "chain_id"), ("event_chain_v1", "episode_id"),
              ("chain_links_v1", "src_episode"), ("chain_links_v1", "dst_episode"),
              ("chain_links_v1", "chain_id")]:
        assert types[k] == "VARCHAR", (k, types[k])
    allnull = pd.DataFrame({"chain_id": [None], "episode_id": [None], "order": [1],
                            "link_score": [0.5], "n_topics": [1], "n_platforms": [1],
                            "n_authors": [1]})
    M08.publish_optional_tables(con, {"event_chain_v1": allnull})
    assert con.execute("SELECT typeof(chain_id) FROM event_chain_v1").fetchone()[0] == "VARCHAR"
    tabs = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert "chain_links_v1" not in tabs, "an unstaged optional table must be dropped"
    M08.publish_optional_tables(con, {})
    assert not {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def _synthetic_src(seed=21):
    """A load_source()-shaped dict (v4_ext + ext) built from synthetic posts only."""
    rng = np.random.default_rng(seed)
    d0 = date(2026, 6, 1)
    n_days = 70
    topics = [f"t{j}" for j in range(6)]
    rows = []
    for d in range(n_days):
        for pl in ("telegram", "reddit"):
            for t in topics:
                lam = 4.0 + (25.0 if (t == "t1" and 40 <= d < 46) else 0.0)
                for _ in range(rng.poisson(lam)):
                    rows.append((pl, t, d0 + timedelta(days=d)))
    n = len(rows)
    pl, tp, dy = map(list, zip(*rows))
    ts = pd.to_datetime([str(x) for x in dy], utc=True) + pd.to_timedelta(
        rng.integers(0, 86400, n), unit="s")
    tags = rng.choice(["#alpha", "#beta", "#gamma", ""], n)
    posts = pd.DataFrame({
        "post_uid": [f"p{i:06d}" for i in range(n)], "platform": pl,
        "author_uid": [f"a{k}" for k in rng.integers(0, 150, n)],
        "author_name": None, "is_verified": False,
        "text": [f"body {i % 97} long enough text for dedupe {tg}" for i, tg in enumerate(tags)],
        "lang": "en", "posted_at": ts, "scraped_at": ts + pd.Timedelta(hours=2),
        "scrape_lag_days": 2 / 24.0, "keyword": None, "hashtags": None,
        "likes": rng.integers(0, 50, n).astype(float), "comments": 1.0, "shares": 0.0,
        "views": rng.integers(1, 5000, n).astype(float), "has_engagement": True,
        "topic_id": pd.Series(tp, dtype=object), "confidence": rng.random(n),
        "tone_score": rng.normal(0, 1, n), "tone_valid": True,
        "hate_score": rng.random(n), "hate_valid": True,
        "readability_score": rng.uniform(0, 100, n), "readability_valid": True,
    })
    posts["day_is_scrape_proxy"] = False
    posts["day"] = posts["posted_at"].dt.date
    ext = _ext_from_posts(posts, seed=seed + 1)
    ext["margin"] = rng.uniform(0.0, 0.2, n)
    mat = rng.normal(size=(n, 16)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    parent = dict(zip(ext["topic_id"], ext["parent_topic_id"]))
    return {
        "posts": posts,
        "topic_dim": pd.DataFrame({"topic_id": topics, "domain": [parent[t] for t in topics],
                                   "topic": topics, "size": 1, "top_terms": "x"}),
        "effort_ce": pd.DataFrame(columns=["platform", "day", "active_channels"]),
        "extract": pd.DataFrame({"extract_id": ["synthetic"]}),
        "emb": {"uid": posts["post_uid"].to_numpy(), "mat": mat,
                "index": {u: i for i, u in enumerate(posts["post_uid"])}},
        "v1": pd.DataFrame(), "ext": ext,
    }


def test_main_end_to_end_soft_hmm_chains_synthetic():
    """main() with every switch on, load_source monkeypatched: no DB is opened."""
    saved = copy.deepcopy(M07.PARAMS)
    orig_load = M07.load_source
    src = _synthetic_src()
    M07.load_source = lambda: src
    M07.PARAMS.update({"permutation_n": 19, "chain_null_n": 19, "window_end": None})
    try:
        with tempfile.TemporaryDirectory() as td:
            outs = {}
            for tag, extra in (("hard", []),
                               ("soft", ["--topic-source", "v4_ext", "--count-mode", "soft",
                                         "--burst-method", "hmm", "--chain-method", "v1"])):
                M07.PARAMS.update({k: saved[k] for k in LEGACY})
                sd = Path(td) / tag
                assert M07.main(extra + ["--stage-dir", str(sd), "--tag", tag]) == 0
                outs[tag] = {p.stem: pd.read_parquet(p) for p in sd.glob("*.parquet")}
                outs[tag]["_soft_json"] = (sd / "soft_constants.json").exists()
            h, s = outs["hard"], outs["soft"]
            assert {"event_chain_v1", "chain_links_v1"} <= set(s) and "event_chain_v1" not in h
            assert s["_soft_json"] and not h["_soft_json"]
            n_win = len(src["posts"][src["posts"]["day"] >= date(2026, 6, 14)])
            md = s["metrics_daily"]
            assert abs(md.loc[md["in_window"], "V"].sum() - n_win) < 1e-6
            assert set(md.columns) - set(h["metrics_daily"].columns) == {
                "V_primary", "n_authors_primary"}
            src08 = (SCRIPTS / "08_metrics_db.py").read_text()
            assert '"V_primary", "n_authors_primary"' in src08, "08 must accept soft columns"
            assert set(s["burst_episodes"]["burst_method"]) <= {"hmm_v1", "poisson_cusum"}
            assert (s["burst_episodes"]["burst_method"] == "hmm_v1").any()
            pj = __import__("json").loads(s["run_meta"]["params_json"].iloc[0])
            assert pj["tag"] == "soft" and s["run_meta"]["run_id"].iloc[0].endswith("_soft")
            assert set(pj["hmm_fits"]["topic_platform"]) == {"telegram", "reddit"}
            assert "first half" in pj["soft_topics"]["constants_source"]
            assert "event_chains" in pj and "circular_shift_null" in pj["event_chains"]
            pjh = __import__("json").loads(h["run_meta"]["params_json"].iloc[0])
            assert not {"soft_topics", "hmm_fits", "event_chains"} & set(pjh)
    finally:
        M07.load_source = orig_load
        M07.PARAMS.clear()
        M07.PARAMS.update(saved)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n  {'all passed' if not fails else f'{fails} failed'}")
    sys.exit(1 if fails else 0)
