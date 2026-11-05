"""Read-only status for the live Gemma routing / NER pipeline (GET /api/gemma/status).

Opens data/gemma_ner.duckdb and data/incoming_posts.duckdb READ-ONLY with a short
retry (writers hold them briefly). Never raises: a missing/locked file yields
{"available": false, "error": "..."}.

Path overrides (env): GEMMA_NER_DB, INCOMING_DB, ANALYTICS_DB, ENTITY_SCHEMA_JSON.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_TTL_S = 3.0
_cache: dict = {"t": 0.0, "v": None}
_lock = threading.Lock()


def _paths():
    g = lambda k, d: Path(os.environ.get(k) or d)
    return (g("GEMMA_NER_DB", DATA / "gemma_ner.duckdb"),
            g("INCOMING_DB", DATA / "incoming_posts.duckdb"),
            g("ANALYTICS_DB", DATA / "analytics.duckdb"),
            g("ENTITY_SCHEMA_JSON", DATA / "entity_schema.json"))


def _open_ro(path: Path, tries: int = 3, delay: float = 0.5):
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found (pipeline not running yet)")
    last = None
    for i in range(tries):
        try:
            return duckdb.connect(str(path), read_only=True)
        except Exception as e:  # writer holds the lock
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise RuntimeError(f"{path.name} busy: {last}")


def _domain_names(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return {k: v.get("name", k) for k, v in (d.get("l2") or {}).items()}
    except Exception:
        return {}


def _compute() -> dict:
    ner, inc, ana, schema = _paths()
    now = datetime.now(timezone.utc)
    out = {"available": False, "updated_at": now.isoformat()}
    try:
        gcon = _open_ro(ner)
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        icon = _open_ro(inc)
    except Exception as e:
        gcon.close()
        out["error"] = str(e)
        return out
    try:
        q = lambda c, s, p=(): c.execute(s, list(p)).fetchall()

        def qt(c, s, p=(), default=None):
            # entity tables only exist once script 18 has run; routing progress must show before that
            try:
                return q(c, s, p)
            except Exception as e:
                if "does not exist" in str(e):
                    return default
                raise
        routed, ok_n = q(gcon, "SELECT count(*), count(*) FILTER (WHERE ok) FROM post_route_v1")[0]
        cut10 = (now.timestamp() - 600)
        recent10 = q(gcon, "SELECT count(*) FROM post_route_v1 WHERE try_cast(routed_at AS TIMESTAMPTZ) >= to_timestamp(?)", [cut10])[0][0]
        ext_posts = qt(gcon, "SELECT count(*) FROM post_entities_status_v2 WHERE ok", default=[(0,)])[0][0]
        ents = qt(gcon, "SELECT count(*) FROM entity_mentions_v2", default=[(0,)])[0][0]
        by_domain = {(d or "none"): n for d, n in q(
            gcon, "SELECT domain_1, count(*) FROM post_route_v1 WHERE ok GROUP BY 1 ORDER BY 2 DESC")}
        by_level = {"L1": 0, "L2": 0}
        for l, n in qt(gcon, "SELECT level, count(*) FROM entity_mentions_v2 GROUP BY 1", default=[]):
            by_level[l] = n
        cut24 = now.timestamp() - 86400
        top = [{"name": n, "entity_type": t, "level": l, "n": c} for n, t, l, c in qt(
            gcon, """SELECT coalesce(canonical_name, name), entity_type, level, count(*) c
                     FROM entity_mentions_v2
                     WHERE try_cast(extracted_at AS TIMESTAMPTZ) >= to_timestamp(?)
                     GROUP BY 1,2,3 ORDER BY c DESC, 1 LIMIT 15""", [cut24], default=[])]
        rec = q(gcon, """SELECT post_uid, platform, domain_1, domain_2, event_mode, confidence_1, routed_at
                         FROM post_route_v1 ORDER BY routed_at DESC LIMIT 25""")
        uids = [r[0] for r in rec]
        ents_by: dict = {u: [] for u in uids}
        if uids:
            ph = ",".join("?" * len(uids))
            for u, t, n, l in qt(gcon, f"""SELECT post_uid, entity_type, coalesce(canonical_name,name), level
                                          FROM entity_mentions_v2 WHERE post_uid IN ({ph})
                                          ORDER BY level, entity_type, 3""", uids, default=[]):
                ents_by[u].append({"type": t, "name": n, "level": l})
        # text + new-vs-backlog
        texts, new = {}, set()
        if uids:
            ph = ",".join("?" * len(uids))
            for u, t in q(icon, f"SELECT post_uid, text FROM incoming_posts WHERE post_uid IN ({ph})", uids):
                texts[u] = t
                new.add(u)
        missing = [u for u in uids if u not in new]
        if missing and ana.exists():
            try:
                acon = _open_ro(ana, tries=2)
                try:
                    ph = ",".join("?" * len(missing))
                    for u, t in q(acon, f"SELECT post_uid, text FROM post_facts WHERE post_uid IN ({ph})", missing):
                        texts[u] = t
                finally:
                    acon.close()
            except Exception:
                pass  # snippets are best-effort
        pending = 0
        routed_ok = {r[0] for r in q(gcon, "SELECT post_uid FROM post_route_v1 WHERE ok")}
        for (u,) in q(icon, "SELECT post_uid FROM incoming_posts"):
            if u not in routed_ok:
                pending += 1
        ws = q(icon, "SELECT max(last_poll), coalesce(sum(new_seen),0) FROM watch_state")[0]
        out.update({
            "available": True,
            "routed": routed,
            "extracted_posts": ext_posts,
            "entities": ents,
            "ok_rate": (ok_n / routed) if routed else None,
            "rate_per_min": recent10 / 10.0,
            "incoming_pending": pending,
            "by_domain": by_domain,
            "domain_names": _domain_names(schema),
            "by_level": by_level,
            "top_entities": top,
            "recent": [{
                "post_uid": u, "platform": p,
                "snippet": " ".join((texts.get(u) or "").split())[:180],
                "domain_1": d1, "domain_2": d2, "event_mode": em, "confidence_1": c1,
                "routed_at": ra, "source": "new" if u in new else "backlog",
                "entities": ents_by.get(u, []),
            } for u, p, d1, d2, em, c1, ra in rec],
            "watcher": {"last_poll": ws[0], "new_seen_total": int(ws[1])},
        })
    except Exception as e:
        out["available"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        gcon.close()
        icon.close()
    return out


def gemma_status(use_cache: bool = True) -> dict:
    with _lock:
        now = time.monotonic()
        if use_cache and _cache["v"] is not None and now - _cache["t"] < CACHE_TTL_S:
            return _cache["v"]
        v = _compute()
        _cache.update(t=now, v=v)
        return v
