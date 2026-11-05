"""Live, rolling-window view for the dashboard (GET /api/live/overview).

Unlike the static metrics snapshot, this is computed per request (cached a few
seconds) from read-only DuckDB files, so it follows whatever the pipeline has
produced:

  * window  : the last `days` (default 7) ending today. If the corpus has not
              reached today yet, the window is anchored on the newest data day
              and `stale_days` says how far behind it is -- never an empty chart.
  * domains : Gemma routing (data/gemma_ner.duckdb `post_route_v1`) -- the domain
              list comes from data/entity_schema.json, so a new D-code appears
              without a code change.
  * military: posts whose primary or secondary routed domain is in the requested
              military-related set (default D05 Military & Defence plus D01, D04,
              D06, D09), with daily volume, top entities and recent posts.

Provisional: routing precision has not been judged yet (see current-arch.md).
Never raises: a missing or locked file yields {"available": false, "error": ...}.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_TTL_S = 10.0
DEFAULT_MILITARY = ("D05", "D01", "D04", "D06", "D09")
MAX_DAYS = 90
_cache: dict = {}
_lock = threading.Lock()


def _p(env, default):
    return Path(os.environ.get(env) or default)


def _open_ro(path: Path, tries: int = 3, delay: float = 0.4):
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found")
    last = None
    for i in range(tries):
        try:
            return duckdb.connect(str(path), read_only=True)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise RuntimeError(f"{path.name} busy: {last}")


def _schema() -> dict:
    try:
        return json.loads(_p("ENTITY_SCHEMA_JSON", DATA / "entity_schema.json").read_text())
    except Exception:
        return {}


def _compute(days: int, military: tuple[str, ...], today: date) -> dict:
    out = {"available": False, "today": today.isoformat(),
           "updated_at": datetime.now(timezone.utc).isoformat()}
    schema = _schema()
    names = {k: v.get("name", k) for k, v in (schema.get("l2") or {}).items()}
    buckets = {k: v.get("bucket") for k, v in (schema.get("l2") or {}).items()}
    military = tuple(m for m in military if m in names) or tuple(
        m for m in DEFAULT_MILITARY if m in names)

    try:
        con = _open_ro(_p("ANALYTICS_DB", DATA / "analytics.duckdb"))
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        gner = _p("GEMMA_NER_DB", DATA / "gemma_ner.duckdb")
        routed_ok = True
        try:
            g = _open_ro(gner)
        except Exception as e:
            g, routed_ok, out["routing_error"] = None, False, str(e)
        try:
            # cast to VARCHAR first: reading TIMESTAMPTZ natively needs pytz
            d_end = con.execute(
                "SELECT max(CAST(CAST(coalesce(posted_at, scraped_at) AS VARCHAR) AS DATE)) "
                "FROM post_facts").fetchone()[0]
            if d_end is None:
                out["error"] = "no posts"
                return out
            anchor = min(today, d_end)
            start = anchor - timedelta(days=days - 1)
            prev_start = start - timedelta(days=days)
            days_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]
            out.update({"data_end": d_end.isoformat(), "anchor": anchor.isoformat(),
                        "from": start.isoformat(), "to": anchor.isoformat(),
                        "stale_days": max(0, (today - d_end).days), "days": days_list})

            day_expr = "CAST(CAST(coalesce(posted_at, scraped_at) AS VARCHAR) AS DATE)"
            tot = {str(d): n for d, n in con.execute(
                f"SELECT {day_expr} d, count(*) FROM post_facts WHERE {day_expr} BETWEEN ? AND ? "
                "GROUP BY 1", [start, anchor]).fetchall()}
            prev_total = con.execute(
                f"SELECT count(*) FROM post_facts WHERE {day_expr} BETWEEN ? AND ?",
                [prev_start, start - timedelta(days=1)]).fetchone()[0]
            out["volume"] = {"daily": [tot.get(d, 0) for d in days_list],
                             "total": sum(tot.values()), "prev_total": prev_total}

            if g is None:
                out.update({"available": True, "domains": [], "military": None,
                            "domain_names": names})
                return out

            # routed posts in the window, joined to post day on the analytics side
            uids = con.execute(
                f"SELECT post_uid, {day_expr}, platform FROM post_facts WHERE {day_expr} "
                "BETWEEN ? AND ?", [prev_start, anchor]).fetchall()
            g.execute("CREATE TEMP TABLE w(post_uid VARCHAR, day DATE, platform VARCHAR)")
            g.executemany("INSERT INTO w VALUES (?,?,?)", uids)
            routed = g.execute(
                "SELECT w.post_uid, w.day, w.platform, r.domain_1, r.domain_2, r.confidence_1, "
                "r.event_mode FROM w JOIN post_route_v1 r USING (post_uid) WHERE r.ok").fetchall()
            win = [r for r in routed if r[1] >= start]
            prev = [r for r in routed if r[1] < start]
            idx = {d: i for i, d in enumerate(days_list)}
            n_posts_win = out["volume"]["total"]
            routed_daily = [0] * days
            for r in win:
                routed_daily[idx[str(r[1])]] += 1
            out["coverage"] = {"routed_daily": routed_daily,
                               "routed_in_window": len(win), "posts_in_window": n_posts_win,
                               "share": (len(win) / n_posts_win) if n_posts_win else None}

            def new_series():
                return [0] * days

            dom = {}
            for _, d, _, d1, *_ in win:        # primary domain only, so shares sum to 100%
                s = dom.setdefault(d1 or "none", {"n": 0, "daily": new_series()})
                s["n"] += 1
                s["daily"][idx[str(d)]] += 1
            prev_dom: dict = {}
            for _, _, _, d1, *_ in prev:
                prev_dom[d1 or "none"] = prev_dom.get(d1 or "none", 0) + 1
            out["domains"] = sorted(
                [{"domain": k, "name": names.get(k, "Not security-related" if k == "none" else k),
                  "bucket": buckets.get(k), "n": v["n"], "prev_n": prev_dom.get(k, 0),
                  "daily": v["daily"]} for k, v in dom.items()],
                key=lambda r: -r["n"])

            # military mode
            mset = set(military)
            # primary domain only: a runner-up match is weaker (game ads, memes) and is
            # reported separately as `also_touches` instead of inflating the count
            m_win = [r for r in win if r[3] in mset]
            also = sum(1 for r in win if r[3] not in mset and r[4] in mset)
            m_daily = new_series()
            plat: dict = {}
            for _, d, p, *_ in m_win:
                m_daily[idx[str(d)]] += 1
                plat[p] = plat.get(p, 0) + 1
            m_prev = sum(1 for r in prev if r[3] in mset)
            m_uids = [r[0] for r in m_win]
            ents, recent = [], []
            if m_uids:
                g.execute("CREATE TEMP TABLE m(post_uid VARCHAR)")
                g.executemany("INSERT INTO m VALUES (?)", [(u,) for u in m_uids])
                try:
                    ents = [{"name": n, "entity_type": t, "level": l, "n": c, "posts": pc}
                            for n, t, l, c, pc in g.execute(
                                """SELECT coalesce(canonical_name, name), entity_type, level,
                                          count(*) c, count(DISTINCT post_uid) pc
                                   FROM entity_mentions_v2 JOIN m USING (post_uid)
                                   GROUP BY 1,2,3 ORDER BY pc DESC, 1 LIMIT 20""").fetchall()]
                except Exception as e:
                    if "does not exist" not in str(e):
                        raise
                top = sorted(m_win, key=lambda r: (str(r[1]), r[5] or 0), reverse=True)[:12]
                texts = {}
                tu = [r[0] for r in top]
                if tu:
                    ph = ",".join("?" * len(tu))
                    texts = {u: (t, ts) for u, t, ts in con.execute(
                        f"SELECT post_uid, text, CAST(coalesce(posted_at, scraped_at) AS VARCHAR) "
                        f"FROM post_facts WHERE post_uid IN ({ph})", tu).fetchall()}
                recent = [{"post_uid": r[0], "day": str(r[1]), "platform": r[2],
                           "domain": r[3], "domain_2": r[4], "confidence": r[5],
                           "event_mode": r[6],
                           "text": (texts.get(r[0], ("", ""))[0] or "")[:280],
                           "at": texts.get(r[0], ("", ""))[1]} for r in top]
            out["military"] = {
                "domains": [{"domain": m, "name": names.get(m, m)} for m in military],
                "n": len(m_win), "also_touches": also, "prev_n": m_prev, "daily": m_daily,
                "share_of_routed": (len(m_win) / len(win)) if win else None,
                "platforms": sorted(({"platform": k, "n": v} for k, v in plat.items()),
                                    key=lambda r: -r["n"]),
                "entities": ents, "recent": recent}
            out["domain_names"] = names
            out["available"] = True
            return out
        finally:
            if g is not None:
                g.close()
    finally:
        con.close()


def live_overview(days: int = 7, domains: tuple[str, ...] = DEFAULT_MILITARY,
                  today: date | None = None) -> dict:
    days = max(1, min(int(days), MAX_DAYS))
    today = today or datetime.now(timezone.utc).date()
    key = (days, tuple(domains), today)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        try:
            v = _compute(days, tuple(domains), today)
        except Exception as e:
            v = {"available": False, "error": f"{type(e).__name__}: {e}"}
        _cache[key] = (time.time(), v)
        return v
