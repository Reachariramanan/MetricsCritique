#!/usr/bin/env python3
"""
Serves the dashboard's static files AND a read-only drill-down API.

Every clickable element in the UI (a topic bar, an entity name, a risk/theme
card, an alert row, a platform panel...) calls GET /api/drill here instead of
anything being preloaded into browser memory up front. Each call:
  - opens a short-lived cursor against the existing read-only connection
  - runs exactly one bounded (LIMIT-capped) query selecting only the columns
    that view needs
  - returns JSON and is done -- nothing is cached or accumulated server-side
    between requests, and the browser discards the rows when the drill panel
    is closed. Memory use is "one query's worth of rows", not "the corpus".
  - also attaches a small `related` block (co-occurring entities/categories/
    topics + a few similar posts) computed the same way -- see compute_related().

Usage:
    cd UI && ../.venv/bin/python3 api_server.py [port]   # default 8877
"""
from __future__ import annotations

import json
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import duckdb

from gemma_live import gemma_status
from live_view import live_overview, DEFAULT_MILITARY

HERE = Path(__file__).resolve().parent
EDA_ROOT = HERE.parent
DATA = EDA_ROOT / "data"

DEFAULT_LIMIT = 50
MAX_LIMIT = 300

_main_con = duckdb.connect(database=":memory:", read_only=False)
_main_con.execute(f"ATTACH '{DATA / 'analytics.duckdb'}' AS analytics (READ_ONLY)")
_main_con.execute(f"ATTACH '{DATA / 'metrics.duckdb'}' AS metrics (READ_ONLY)")
_main_con.execute(f"ATTACH '{DATA / 'geo_ontology.duckdb'}' AS geo (READ_ONLY)")

POST_COLS = "p.post_uid, p.platform, p.posted_at, p.author_name, left(coalesce(p.text,''), 500) AS text, p.url"


def _limit(qs: dict) -> int:
    try:
        n = int(qs.get("limit", [DEFAULT_LIMIT])[0])
    except (ValueError, TypeError):
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _day_clause(qs, where, params, col="p.posted_at"):
    day = qs.get("day", [None])[0]
    if day:
        where.append(f"CAST({col} AS DATE) = ?")
        params.append(day)


def _platform_clause(qs, where, params, col="p.platform"):
    platform = qs.get("platform", [None])[0]
    if platform:
        where.append(f"{col} = ?")
        params.append(platform)


class NotYetPopulated(Exception):
    """A well-formed request against a declared-but-not-yet-built table.

    Distinct from ValueError (malformed request) so the handler can answer 503
    "not ready" rather than 400 "you asked wrong" -- the caller did nothing
    wrong and retrying after the pipeline runs will succeed.
    """


def _has_table(name: str, _cache: dict = {}) -> bool:
    """Does `geo.<name>` exist? Cached, because this is checked per request and
    the answer only changes when a pipeline stage runs."""
    if name not in _cache:
        _cache[name] = bool(_main_con.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = 'geo' AND table_name = ?", [name]).fetchone()[0])
    return _cache[name]


# Which clustering the topic views serve. v4 is hierarchical: parent topics
# (p0x, level 1) over sub-topics (h0xx, level 2), rebuilt by
# scripts/15_topic_clustering_v4.py. Bumping this is the whole switch -- the
# queries below read it rather than hardcoding a table name, which is what made
# the v3 -> v4 move a one-line change instead of a grep-and-pray.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
from topic_version import TOPIC_DIM_TABLE as TOPIC_DIM, TOPIC_POST_TABLE as TOPIC_POST  # noqa: E402


def _resolve_code(key: str) -> tuple[str, str]:
    """Accept either the canonical code_key ('a05_maritime') or the bare CAMEO
    code ('05'); return (code_key, code).

    code_key is canonical for display and routing: bare numeric codes repeat
    across axes and levels, which is precisely the collision the code_key scheme
    exists to prevent, and it is what the taxonomy axes are keyed by -- so an API
    that emitted '05' would make every UI label fall back to the raw string.
    Hierarchy, however, is expressed in the numeric code ('05' -> '05.2'), so
    subtree queries need that form. Both are carried rather than picking one.
    Unknown keys pass through unchanged so the caller still gets an empty result
    rather than an error."""
    if not key or not _has_table("event_codes"):
        return key, key
    row = _main_con.execute(
        "SELECT code_key, code FROM geo.event_codes WHERE code_key = ? OR code = ? LIMIT 1",
        [key, key]).fetchone()
    return (row[0], row[1]) if row else (key, key)


# Which drill view serves each geo_alert_candidates.dimension_type, and which
# query parameter carries the key. This is the extension point: a new dimension
# needs one line here, not a new branch inside the alert router.
ALERT_ROUTES: dict[str, tuple[str, str]] = {
    "entity": ("entity", "entity_uid"),
    "risk": ("risk", "category"),
    "theme": ("theme", "category"),
    "theme_code": ("theme_code", "code"),
    "theme_subcode": ("theme_subcode", "code"),
    "action": ("action", "code"),
}


def build_query(view: str, qs: dict):
    """Returns (sql, params) for a given drill view + querystring dict, or raises ValueError."""
    where, params = [], []

    if view == "topic":  # metrics v2 topic_id (t0xx), from analytics.post_topic
        topic_id = qs.get("topic_id", [None])[0]
        if not topic_id:
            raise ValueError("topic_id required")
        where.append("pt.topic_id = ?")
        params.append(topic_id)
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, pt.confidence, count(*) OVER () AS total_rows
            FROM analytics.post_topic pt
            JOIN analytics.post_facts p ON p.post_uid = pt.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view in ("topic_v3", "topic_v4"):
        # Hierarchical topics from scripts/15_topic_clustering_v4.py.
        # `topic_v3` is kept as an alias so links minted before the v4 switch
        # keep resolving -- they carry cluster_id, which still means "a cluster".
        # NB: "topic" is NOT accepted here -- it is already taken by the metrics
        # v2 keyword topics (t0xx, analytics.post_topic) handled above.
        cluster_id = qs.get("cluster_id", [None])[0]
        topic_id = qs.get("topic_id", [None])[0]
        if topic_id and str(topic_id).startswith("p"):
            # A parent drill returns the union of its sub-topics' posts, the same
            # contract as a CAMEO parent-code drill. Clicking a parent that showed
            # nothing would be the obvious wrong answer.
            where.append("pt.parent_topic_id = ?")
            params.append(topic_id)
        elif topic_id:
            where.append("pt.topic_id = ?")
            params.append(topic_id)
        elif cluster_id is not None:
            where.append("pt.cluster_id = ?")
            params.append(int(cluster_id))
        else:
            raise ValueError("cluster_id or topic_id required")
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, pt.confidence, count(*) OVER () AS total_rows
            FROM geo.{TOPIC_POST} pt
            JOIN analytics.post_facts p ON p.post_uid = pt.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view == "tag":
        tag_id = qs.get("tag_id", [None])[0]
        if not tag_id:
            raise ValueError("tag_id required")
        where.append("td.tag_id = ?")
        params.append(tag_id)
        # tag_dim is mined (07_metrics_v2.py::mine_tags) from the UNION of the structured
        # hashtags field and inline #tags in post text -- approximate both here rather than
        # only the structured field, or most tags would look nearly empty on drill-down.
        where.append("""(
            lower(coalesce(p.hashtags,'')) LIKE ('%"' || lower(td.tag) || '"%')
            OR regexp_matches(lower(coalesce(p.text,'')), '(^|[^[:alnum:]_])#' || lower(td.tag) || '([^[:alnum:]_]|$)')
        )""")
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, count(*) OVER () AS total_rows
            FROM metrics.tag_dim td, analytics.post_facts p
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view == "entity":
        entity_uid = qs.get("entity_uid", [None])[0]
        if not entity_uid:
            raise ValueError("entity_uid required")
        where.append("em.entity_uid = ?")
        params.append(entity_uid)
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, em.surface_form, em.entity_subtype, count(*) OVER () AS total_rows
            FROM geo.entity_mentions em
            JOIN analytics.post_facts p ON p.post_uid = em.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view == "entity_subtype":  # taxonomy card drill: every mention of a given subtype, any entity
        subtype = qs.get("entity_subtype", [None])[0]
        if not subtype:
            raise ValueError("entity_subtype required")
        where.append("em.entity_subtype = ?")
        params.append(subtype)
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, em.surface_form, em.entity_subtype, count(*) OVER () AS total_rows
            FROM geo.entity_mentions em
            JOIN analytics.post_facts p ON p.post_uid = em.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view in ("risk", "theme"):
        table = "geo.risk_labels" if view == "risk" else "geo.theme_labels"
        category = qs.get("category", [None])[0]
        if not category:
            raise ValueError("category required")
        where.append("rl.category = ?")
        params.append(category)
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, rl.score, rl.gray_zone, count(*) OVER () AS total_rows
            FROM {table} rl
            JOIN analytics.post_facts p ON p.post_uid = rl.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY rl.score DESC
        """
        return sql, params

    if view == "platform":
        platform = qs.get("platform", [None])[0]
        if not platform:
            raise ValueError("platform required")
        where.append("p.platform = ?")
        params.append(platform)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, count(*) OVER () AS total_rows
            FROM analytics.post_facts p
            WHERE {' AND '.join(where)}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view == "posts":  # generic corpus browse: optional platform/day, no dimension required
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, count(*) OVER () AS total_rows
            FROM analytics.post_facts p
            {'WHERE ' + ' AND '.join(where) if where else ''}
            ORDER BY p.posted_at DESC
        """
        return sql, params

    if view in ("theme_code", "theme_subcode", "action"):
        # CAMEO event codes (scripts/14_cameo_event_coding.py). One table with an
        # `axis` column rather than three, so a hierarchy query is a single scan.
        if not _has_table("event_codes"):
            # Not a 400. The taxonomy declares these axes before the coding pass
            # has populated them (status=declared_not_yet_populated), so "not yet
            # coded" is an expected state, not a malformed request. Returning an
            # error here would make a correctly-declared-but-unpopulated axis
            # indistinguishable from a bug.
            raise NotYetPopulated(
                "event_codes has not been built yet -- run "
                "scripts/14_cameo_event_coding.py --level parent")
        code = qs.get("code", [None])[0] or qs.get("category", [None])[0]
        if not code:
            raise ValueError("code required")
        code_key, code_num = _resolve_code(code)
        axis = "action" if view == "action" else "theme"
        where.append("ec.axis = ?")
        params.append(axis)
        if view == "theme_code":
            # A parent drill must return its whole subtree. Exact equality would
            # show 05 as if it had no 05.x children, which is the opposite of
            # what a user clicking a parent expects. Matching is on the *numeric*
            # code because that is where the hierarchy lives ('05' -> '05.2');
            # code_key does not nest lexically (a05_maritime is not a prefix of
            # a05_2_maritime_disputes).
            where.append("(ec.code_key = ? OR ec.code LIKE ? || '.%')")
            params.extend([code_key, code_num])
        else:
            where.append("ec.code_key = ?")
            params.append(code_key)
        _platform_clause(qs, where, params)
        _day_clause(qs, where, params)
        sql = f"""
            SELECT {POST_COLS}, ec.score, ec.code, ec.gray_zone,
                   ec.threshold_source, count(*) OVER () AS total_rows
            FROM geo.event_codes ec
            JOIN analytics.post_facts p ON p.post_uid = ec.post_uid
            WHERE {' AND '.join(where)}
            ORDER BY ec.score DESC, ec.post_uid
        """
        return sql, params

    if view == "alert":
        # Routes a geo_alert_candidates row (dimension_type + dimension_key + day)
        # to the matching drill view, pre-filtered to that alert's day.
        dtype = qs.get("dimension_type", [None])[0]
        dkey = qs.get("dimension_key", [None])[0]
        if not dtype or not dkey:
            raise ValueError("dimension_type and dimension_key required")
        route = ALERT_ROUTES.get(dtype)
        if route is None:
            # Was a hard `raise ValueError`, which made this the most brittle
            # coupling in the repo: adding ANY new dimension upstream 400'd every
            # drill on it until someone remembered to edit this function. The
            # registry above is the extension point; an unknown type now names
            # itself and lists what IS routable, instead of just failing.
            raise ValueError(
                f"unknown dimension_type {dtype!r}; routable types are "
                f"{sorted(ALERT_ROUTES)}. Add it to ALERT_ROUTES in api_server.py.")
        sub_view, param = route
        sub_qs = dict(qs)
        sub_qs[param] = [dkey]
        return build_query(sub_view, sub_qs)

    raise ValueError(f"unknown view {view!r}")


# ---------------------------------------------------------------- related --
# "Related items" for the drill panel: co-occurring entities, co-occurring
# risk/theme categories, related topics (the served clustering -- the more
# refined of the two topic ontologies in this project), and a handful of
# similar posts (single-seed cosine similarity via DuckDB's built-in
# list_cosine_similarity against the existing bge-m3 embeddings -- no
# separate cache, no new dependency, same one-bounded-query-per-bit-of-work
# style as the rest of this file). Co-occurrence is normalized by the
# candidate's own overall frequency (a lift-style score), not raw counts, or
# every card would just surface the globally most-common entities.
#
# Best-effort: any failure here must never break the main `rows` response
# (see do_GET), so this returns partial/empty results rather than raising
# where practical.

RELATED_LIMIT = 6
SIMILAR_LIMIT = 5

# Which event_codes (axis, level) each CAMEO dimension covers. Candidates are
# emitted under these dim_type names so a click routes straight back through
# ALERT_ROUTES. level=None means "any" (the action axis is flat).
CAMEO_RELATED_AXES: dict[str, tuple[str, int | None]] = {
    "theme_code": ("theme", 1),
    "theme_subcode": ("theme", 2),
    "action": ("action", None),
}
RELATED_MIN_PER_AXIS = 2  # see _per_axis_cap
SEL_SCOPE_LIMIT = 2000  # cap on how many posts back the co-occurrence stats, independent of the small `rows` limit

# Association scoring. The first version of this divided a *mention* count by a
# *post* count (units mismatch, scores could exceed 1.0) with only a 2-mention
# floor, which meant an entity seen in one post outranked everything: for
# entity=Iran all six slots were singletons (Somaliland 6 mentions / 1 post),
# while the real partners (Red Sea, Yemen, Strait of Hormuz) never appeared.
#
# Now: count DISTINCT posts on both sides, score by lift against the corpus
# base rate, and shrink toward 1.0 by co-occurrence support so tiny counts
# cannot dominate. Floors below are deliberately blunt -- they exist to keep
# one-post coincidences out of a 6-slot list, not to be finely tuned.
MIN_CO_POSTS = 3       # candidate must co-occur in at least this many distinct posts
MIN_GLOBAL_POSTS = 10  # ...and be common enough that its base rate is meaningful
SHRINK_K = 5.0         # lift * co/(co+k): damps small-sample lift spikes


def _current_topic_v3_cluster(qs: dict) -> int:
    cid = qs.get("cluster_id", [None])[0]
    if cid is not None:
        try:
            return int(cid)
        except ValueError:
            return -1
    tid = qs.get("topic_id", [None])[0]
    # v4 sub-topics are h0xx and parents p0x; v3 used g0xx. Accept all three so a
    # stale link still self-excludes correctly rather than suggesting itself back.
    if tid and tid[:1] in ("h", "p", "g") and tid[1:].isdigit():
        return int(tid[1:])
    return -1


def _self_exclusions(view: str, qs: dict):
    """(exclude_entity_uid, exclude_cat_dim, exclude_cat_key, exclude_cluster_id,
    (exclude_axis, exclude_code)) for the dimension value currently being viewed, so
    it's never suggested back as its own 'related' item. Empty string / -1 sentinels
    never match a real key.

    The 5th slot is the CAMEO one and is deliberately *not* an exact key: on a
    hierarchical axis, self-exclusion has to cover the whole ancestor/descendant
    chain. A parent code co-occurs with its own child in 100% of the child's posts,
    which is maximal lift by construction -- match only the exact key and code 05
    tops the related list for every one of its sub-codes, in every slot, forever.
    That is a tautology, not an association. The axis (not the dim_type) is what
    scopes it, because theme_code and theme_subcode are two levels of one tree."""
    def cameo(dtype: str, key: str):
        axis, _lvl = CAMEO_RELATED_AXES.get(dtype, ("", None))
        return ("", "", "", -1, (axis, key if axis else ""))

    if view == "alert":
        dtype = qs.get("dimension_type", [None])[0] or ""
        dkey = qs.get("dimension_key", [None])[0] or ""
        if dtype == "entity":
            return dkey, "", "", -1, ("", "")
        if dtype in ("risk", "theme"):
            return "", dtype, dkey, -1, ("", "")
        if dtype in CAMEO_RELATED_AXES:
            return cameo(dtype, dkey)
        return "", "", "", -1, ("", "")
    if view == "entity":
        return qs.get("entity_uid", [None])[0] or "", "", "", -1, ("", "")
    if view in ("risk", "theme"):
        return "", view, qs.get("category", [None])[0] or "", -1, ("", "")
    if view in CAMEO_RELATED_AXES:
        return cameo(view, qs.get("code", [None])[0] or "")
    if view in ("topic_v3", "topic_v4"):
        return "", "", "", _current_topic_v3_cluster(qs), ("", "")
    return "", "", "", -1, ("", "")


def _per_axis_cap(n_axes: int) -> int:
    """How many related slots any single axis may take.

    Sub-codes are the narrowest categories and therefore carry the highest lift,
    so an unbudgeted top-N over the union hands every slot to one axis. Scale the
    cap so the pre-CAMEO behaviour is unchanged (2 axes -> 3 each -> the same 6),
    while a fully-populated 5-axis union guarantees at least three axes appear."""
    if n_axes <= 0:
        return RELATED_LIMIT
    return max(RELATED_MIN_PER_AXIS, -(-RELATED_LIMIT // n_axes))


def compute_related(cur, view: str, qs: dict, seed_post_uid):
    sel_sql, sel_params = build_query(view, qs)
    # Deterministic scope sample. `SELECT DISTINCT ... LIMIT n` discards the
    # inner ORDER BY, so the previous form returned a different arbitrary set
    # on every call -- four identical requests gave four different payloads.
    # hash() ordering is the idiom this project already uses instead of
    # USING SAMPLE (DuckDB reservoir sampling misbehaves with WHERE).
    sel_cte = (
        f"WITH sel AS (SELECT DISTINCT post_uid FROM ({sel_sql}) t "
        f"ORDER BY hash(post_uid) LIMIT {SEL_SCOPE_LIMIT}), "
        f"scope AS (SELECT count(*) AS n FROM sel)"
    )
    excl_entity, excl_cat_dim, excl_cat_key, excl_cluster, (excl_axis, excl_code) = \
        _self_exclusions(view, qs)
    # The viewed key may arrive as either form; resolve once so the exclusion can
    # match code_key exactly and nest on the numeric code.
    excl_code_key, excl_num = _resolve_code(excl_code) if excl_code else ("", "")

    corpus_n = cur.execute("SELECT count(*) FROM analytics.post_facts").fetchone()[0] or 1

    # lift = P(candidate | in scope) / P(candidate | corpus)
    #      = (co_posts / scope_n) / (global_posts / corpus_n)
    # score = lift * co_posts/(co_posts + K)   -- shrink small-sample lift spikes
    entities_df = cur.execute(f"""
        {sel_cte}
        SELECT em.entity_uid,
               e.canonical_name AS name,
               count(DISTINCT em.post_uid) AS co_posts,
               e.distinct_posts AS global_posts,
               ((count(DISTINCT em.post_uid)::DOUBLE / (SELECT n FROM scope))
                 / (e.distinct_posts::DOUBLE / {corpus_n}))
                 * (count(DISTINCT em.post_uid)::DOUBLE
                    / (count(DISTINCT em.post_uid) + {SHRINK_K})) AS score
        FROM sel s
        JOIN geo.entity_mentions em ON em.post_uid = s.post_uid
        JOIN geo.entity_dim e ON e.entity_uid = em.entity_uid
        WHERE em.entity_uid != ?
          AND e.distinct_posts >= {MIN_GLOBAL_POSTS}
          AND e.canonical_name NOT LIKE '%##%'
          AND length(e.canonical_name) > 2
        GROUP BY 1, 2, e.distinct_posts
        HAVING count(DISTINCT em.post_uid) >= {MIN_CO_POSTS}
        ORDER BY score DESC, co_posts DESC, em.entity_uid
        LIMIT {RELATED_LIMIT}
    """, sel_params + [excl_entity]).fetchdf()

    # Axis union. Legacy axes carry axis='' so the hierarchical exclusion below
    # can never fire on them; CAMEO axes are added only once the coding pass has
    # actually written the table, so a half-built pipeline degrades to the
    # pre-CAMEO related list instead of erroring.
    # `key` is what the UI labels and routes by (code_key for CAMEO, category for
    # legacy); `num` is the numeric CAMEO code, carried only so the hierarchical
    # exclusion below can nest -- code_key does not nest lexically.
    co_ctes = [
        "risk_co AS (SELECT 'risk' AS dim, '' AS axis, rl.category AS key, '' AS num, "
        "count(DISTINCT rl.post_uid) AS co_posts FROM sel s "
        "JOIN geo.risk_labels rl ON rl.post_uid = s.post_uid GROUP BY 1, 2, 3, 4)",
        "theme_co AS (SELECT 'theme' AS dim, '' AS axis, tl.category AS key, '' AS num, "
        "count(DISTINCT tl.post_uid) AS co_posts FROM sel s "
        "JOIN geo.theme_labels tl ON tl.post_uid = s.post_uid GROUP BY 1, 2, 3, 4)",
    ]
    total_parts = [
        "SELECT 'risk' AS dim, category AS key, count(DISTINCT post_uid) AS total "
        "FROM geo.risk_labels GROUP BY 1, 2",
        "SELECT 'theme' AS dim, category AS key, count(DISTINCT post_uid) AS total "
        "FROM geo.theme_labels GROUP BY 1, 2",
    ]
    if _has_table("event_codes"):
        for dim, (axis, lvl) in CAMEO_RELATED_AXES.items():
            lvl_co = "" if lvl is None else f" AND ec.level = {int(lvl)}"
            lvl_tot = "" if lvl is None else f" AND level = {int(lvl)}"
            co_ctes.append(
                f"{dim}_co AS (SELECT '{dim}' AS dim, '{axis}' AS axis, "
                f"ec.code_key AS key, ec.code AS num, "
                f"count(DISTINCT ec.post_uid) AS co_posts FROM sel s "
                f"JOIN geo.event_codes ec ON ec.post_uid = s.post_uid "
                f"WHERE ec.axis = '{axis}'{lvl_co} GROUP BY 1, 2, 3, 4)")
            total_parts.append(
                f"SELECT '{dim}' AS dim, code_key AS key, count(DISTINCT post_uid) AS total "
                f"FROM geo.event_codes WHERE axis = '{axis}'{lvl_tot} GROUP BY 1, 2")
    union_co = " UNION ALL ".join(f"SELECT * FROM {c.split(' AS ')[0]}" for c in co_ctes)
    per_axis = _per_axis_cap(len(co_ctes))

    categories_df = cur.execute(f"""
        {sel_cte},
        {', '.join(co_ctes)},
        totals AS ({' UNION ALL '.join(total_parts)}),
        scored AS (
            SELECT c.dim AS dim_type, c.key AS category, c.co_posts,
                   t.total AS global_posts,
                   ((c.co_posts::DOUBLE / (SELECT n FROM scope)) / (t.total::DOUBLE / {corpus_n}))
                     * (c.co_posts::DOUBLE / (c.co_posts + {SHRINK_K})) AS score
            FROM ({union_co}) c
            JOIN totals t ON t.dim = c.dim AND t.key = c.key
            WHERE NOT (c.dim = ? AND c.key = ?)
              AND NOT (c.axis = ? AND (c.key = ?
                                       OR (c.num <> '' AND ? <> ''
                                           AND (? LIKE c.num || '.%'
                                                OR c.num LIKE ? || '.%'))))
              AND c.co_posts >= {MIN_CO_POSTS}
        ),
        ranked AS (
            SELECT *, row_number() OVER (PARTITION BY dim_type
                                         ORDER BY score DESC, co_posts DESC, category) AS rn
            FROM scored
        )
        SELECT dim_type, category, co_posts, global_posts, score
        FROM ranked
        WHERE rn <= {per_axis}
        ORDER BY score DESC, co_posts DESC, dim_type, category
        LIMIT {RELATED_LIMIT}
    """, sel_params + [excl_cat_dim, excl_cat_key,
                       excl_axis, excl_code_key,
                       excl_num, excl_num, excl_num]).fetchdf()

    topics_df = cur.execute(f"""
        {sel_cte}
        SELECT pt.cluster_id, td.topic_id, td.name,
               count(DISTINCT pt.post_uid) AS co_posts,
               td.size AS global_posts,
               ((count(DISTINCT pt.post_uid)::DOUBLE / (SELECT n FROM scope))
                 / (td.size::DOUBLE / {corpus_n}))
                 * (count(DISTINCT pt.post_uid)::DOUBLE
                    / (count(DISTINCT pt.post_uid) + {SHRINK_K})) AS score
        FROM sel s
        JOIN geo.{TOPIC_POST} pt ON pt.post_uid = s.post_uid
        JOIN geo.{TOPIC_DIM} td ON td.cluster_id = pt.cluster_id
        WHERE pt.cluster_id != ? AND pt.cluster_id >= 0 AND td.coherent
        GROUP BY 1, 2, 3, td.size
        HAVING count(DISTINCT pt.post_uid) >= {MIN_CO_POSTS}
        ORDER BY score DESC, co_posts DESC, pt.cluster_id
        LIMIT {RELATED_LIMIT}
    """, sel_params + [excl_cluster]).fetchdf()

    similar_records = []
    if seed_post_uid:
        has_seed = cur.execute(
            "SELECT count(*) FROM analytics.post_embeddings WHERE post_uid = ?", [seed_post_uid]
        ).fetchone()[0]
        if has_seed:
            # Dedup by normalised text before taking the top N. The corpus is
            # heavily templated (repeated air-raid alerts, 125 identical
            # LinkedIn 404 bodies), so without this every slot comes back as a
            # byte-identical copy at similarity 1.000000 -- measured, not
            # hypothetical. Keep the best-scoring post per distinct text.
            similar_records = cur.execute(f"""
                {sel_cte},
                cand AS (
                    SELECT pe.post_uid, p.platform, p.posted_at, p.author_name,
                           left(coalesce(p.text,''), 500) AS text, p.url,
                           list_cosine_similarity(pe.embedding,
                               (SELECT embedding FROM analytics.post_embeddings WHERE post_uid = ?)) AS similarity,
                           md5(lower(regexp_replace(coalesce(p.text,''), '[^[:alnum:]]', '', 'g'))) AS text_key
                    FROM analytics.post_embeddings pe
                    JOIN analytics.post_facts p ON p.post_uid = pe.post_uid
                    WHERE pe.post_uid NOT IN (SELECT post_uid FROM sel) AND pe.post_uid != ?
                ),
                ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY text_key ORDER BY similarity DESC, post_uid
                    ) AS rn
                    FROM cand
                )
                SELECT post_uid, platform, posted_at, author_name, text, url, similarity
                FROM ranked WHERE rn = 1
                ORDER BY similarity DESC, post_uid
                LIMIT {SIMILAR_LIMIT}
            """, sel_params + [seed_post_uid, seed_post_uid]).fetchdf().to_dict(orient="records")

    return {
        "entities": entities_df.rename(columns={"canonical_name": "name"}).to_dict(orient="records"),
        "categories": categories_df.to_dict(orient="records"),
        "topics": topics_df.to_dict(orient="records"),
        "similar_posts": similar_records,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def log_message(self, fmt, *args):
        pass  # quiet; avoid growing a log file for a local dev server

    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({"ok": True})
            return
        if parsed.path == "/api/gemma/status":
            try:
                self._json(gemma_status())
            except Exception as e:  # never 500
                self._json({"available": False, "error": f"{type(e).__name__}: {e}"})
            return
        if parsed.path == "/api/live/overview":
            qs = parse_qs(parsed.query)
            try:
                days = int(qs.get("days", ["7"])[0])
            except ValueError:
                days = 7
            raw = qs.get("domains", [""])[0]
            doms = tuple(d for d in raw.split(",") if d) or DEFAULT_MILITARY
            self._json(live_overview(days, doms))
            return
        if parsed.path == "/api/drill":
            qs = parse_qs(parsed.query)
            view = qs.get("view", [None])[0]
            limit = _limit(qs)
            try:
                sql, params = build_query(view, qs)
                cur = _main_con.cursor()
                try:
                    rows = cur.execute(sql + " LIMIT ?", params + [limit]).fetchdf()
                    total = int(rows["total_rows"].iloc[0]) if len(rows) else 0
                    cols = [c for c in rows.columns if c != "total_rows"]
                    records = rows[cols].to_dict(orient="records")
                    related = None
                    try:
                        # Deterministic seed: 31% of posts share a posted_at with
                        # another post and most views order by it with no
                        # tiebreaker, so records[0] alone was unstable run to run.
                        seed = min((r["post_uid"] for r in records), default=None) if records else None
                        related = compute_related(cur, view, qs, seed)
                    except Exception as e:
                        # Never break the main rows response -- but do not fail
                        # silently either: a permanently broken related query
                        # used to be indistinguishable from "no related items".
                        related = None
                        print(f"WARN: compute_related failed for view={view!r} qs={dict(qs)!r}: "
                              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                finally:
                    cur.close()
                resp = {"view": view, "shown": len(records), "total": total, "rows": records}
                if related is not None:
                    resp["related"] = related
                self._json(resp)
            except NotYetPopulated as e:
                self._json({"error": str(e), "not_yet_populated": True}, status=503)
            except ValueError as e:
                self._json({"error": str(e)}, status=400)
            except Exception as e:
                self._json({"error": f"query failed: {e}"}, status=500)
            return
        super().do_GET()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8877
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving {HERE} + /api/drill on http://0.0.0.0:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
