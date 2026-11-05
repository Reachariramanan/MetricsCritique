#!/usr/bin/env python3
"""
Build the OSINT metrics dashboard.

    python UI/build_dashboard.py

Outputs
-------
    UI/index.html          Slim interactive shell (HTML structure + section copy).
                           Loads chart data from metrics.json at runtime.
                           Filters re-scope charts in the browser (dynamic UI).
                           Not a live DB connection — data is a rebuild snapshot.
    UI/data/metrics.json   Full metrics snapshot (what the UI fetches).
    UI/assets/dash.js|css  Interactive rendering engine.

Serve (required for JSON fetch):
    cd UI && python3 -m http.server 8877

Offline single-file mode:
    python UI/build_dashboard.py --static-embed

Data source
-----------
The script is driven by the schema contract in `docs/METRICS_V2_SPEC.md` §5,
not by hardcoded knowledge of the corpus:

  * if `data/metrics.duckdb` exists it is opened **read-only** and every
    contract table found there is used verbatim (mode `real`);
  * if it does not exist, `UI/fixture.py` generates a seeded stand-in with the
    identical shape (mode `fixture`), so the whole dashboard can be built and
    visually verified before the metrics builder finishes;
  * if the DB exists but is missing some contract tables, the present ones are
    used and the sections that need the absent ones render an explicit
    "awaiting data" state (mode `real (partial)`). Real and fixture data are
    NEVER mixed -- a half-real dashboard that looks complete is worse than a
    visible gap.

Nothing about topic ids, platform names, date ranges or the metric list is
hardcoded. Topics/platforms/tags come from the dimension tables, the day axis
comes from the facts, and every label, formula, unit, direction, caveat and
support gate shown in the UI is read from the `metric_meta` table.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_DB = ROOT / "data" / "metrics.duckdb"
DEFAULT_GEO_DB = ROOT / "data" / "geo_ontology.duckdb"
SPEC = "docs/METRICS_V2_SPEC.md"
GEO_SPEC = "docs/GEOPOLITICAL_ONTOLOGY_SPEC.md"

# Geo-ontology layer (separate DB, separate build from the metrics v2 contract
# above). Additive only: these tables ride alongside the CONTRACT tables in the
# same `tables` dict / same metrics.json, under their own keys, so dash.js can
# read them with the same T('name') accessor. Missing/absent is handled the
# same way as any other optional CONTRACT table -- an explicit "awaiting data"
# state per section, never a fixture stand-in (there is no geo fixture; a real
# gap should look like a gap, not like a demo).
GEO_TABLES = ["entity_dim", "entity_metrics_daily", "risk_metrics_daily",
              "theme_metrics_daily", "geo_alert_candidates"]

sys.path.insert(0, str(HERE))

# The label taxonomy is DATA, loaded here and shipped inside metrics.json, not
# a set of object literals in dash.js. It used to be the latter, and the
# `*_ORDER` arrays there were applied as `ORDER.filter(c => catSet[c])` -- so a
# category present in the data but missing from the array was silently dropped
# from the UI with no error anywhere. Importing the builder (rather than
# reading a checked-in JSON) means the shipped taxonomy can never be stale
# relative to its source. See scripts/lib/taxonomy.py.
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import taxonomy as taxonomy_lib  # noqa: E402
from topic_version import TOPIC_POST_TABLE  # noqa: E402


def load_taxonomy(log=print) -> dict:
    """Build + validate the taxonomy that ships with this snapshot.

    Fails the build on an invalid taxonomy rather than emitting a dashboard
    with mislabelled or missing categories. This is a build-time artifact we
    control end to end, so there is no honest degraded mode: a wrong label is
    worse than no dashboard.
    """
    tax = taxonomy_lib.build()
    errs = taxonomy_lib.validate(tax)
    if errs:
        for e in errs:
            log(f"[ui] TAXONOMY INVALID: {e}")
        raise SystemExit(f"[ui] taxonomy validation failed with {len(errs)} error(s)")
    log("[ui] taxonomy      : " + ", ".join(
        f"{n}={len(a['order'])}" for n, a in tax["axes"].items()))
    return tax

# --------------------------------------------------------------------------
# The contract (docs/METRICS_V2_SPEC.md §5), declared once.
# Column lists are used for *reporting* mismatches, never for indexing --
# whatever columns the DB actually has are what get exported.
# --------------------------------------------------------------------------

CONTRACT = {
    "topic_dim": ["topic_id", "domain", "topic", "label", "n_posts", "description"],
    "tag_dim": ["tag_id", "tag", "n_posts", "platforms", "first_seen", "last_seen"],
    "platform_dim": ["platform", "has_engagement", "in_ts_scope", "notes"],
    "metric_meta": ["metric_key", "display_name", "family", "formula", "units",
                    "grain", "direction", "higher_is_worse", "caveats", "support_gate"],
    "run_meta": ["run_id", "created_at", "source_extract_id", "window_start",
                 "window_end", "params_json"],
    "metrics_daily": [
        "day", "topic_id", "platform", "in_window", "V", "V_rate", "effort",
        "growth", "growth_share", "growth_7d", "accel", "accel_smooth", "momentum",
        "surprise", "surprise_p", "surprise_q", "alert_flag",
        "burst_state", "burst_intensity", "burst_episode_id",
        "novelty", "days_to_threshold", "emergence_score",
        "tone_mean", "tone_eb", "tone_n",
        "hate_mean", "hate_eb", "hate_prevalence", "hate_calibrated",
        "virality_pct", "reach_proxy", "reach_audience_norm",
        "author_entropy", "author_1_hhi", "n_authors", "coordination_flag",
        "readability_mean", "readability_coverage", "support_n", "support_ok"],
    "metrics_topic_day": None,   # "same measures, platform rolled up"
    "tag_metrics_daily": ["day", "tag_id", "platform", "V", "V_rate", "growth",
                          "accel_smooth", "surprise", "surprise_q", "alert_flag",
                          "tone_eb", "hate_eb", "emergence_score", "support_n",
                          "support_ok"],
    "burst_episodes": ["episode_id", "topic_id", "platform", "start_day", "peak_day",
                       "end_day", "peak_V", "total_V", "onset_slope", "inflection_day",
                       "decay_lambda", "half_life_days", "r2", "level"],
    "persistence": ["topic_id", "platform", "ar1_phi", "phi_se", "half_life_days",
                    "volatility", "ljung_box_q", "ljung_box_p", "n_obs"],
    "alert_candidates": ["day", "topic_id", "platform", "tag_id", "metric_key",
                         "value", "surprise_q", "severity", "rationale"],
}

REQUIRED_FOR_BUILD = ["metrics_daily", "metric_meta", "topic_dim", "platform_dim"]

DAY_COLS = {"day", "start_day", "peak_day", "end_day", "inflection_day",
            "window_start", "window_end", "first_seen", "last_seen"}


# ==========================================================================
# loading
# ==========================================================================

def _jsonable(v):
    """Convert one cell to something json.dumps can take, losslessly enough.

    Strict: every non-finite float (incl. numpy float32/64 NaN/Inf) becomes null
    so dumps(..., allow_nan=False) never fails on real metrics tables.
    """
    if v is None:
        return None
    # numpy/pandas scalars first (np.float32 is NOT a Python float subclass)
    if hasattr(v, "item") and not isinstance(v, (bytes, bytearray, memoryview, str)):
        try:
            # only unwrap 0-d numpy / pandas scalars
            if getattr(v, "shape", ()) == () or type(v).__module__.startswith(
                ("numpy", "pandas")
            ):
                return _jsonable(v.item())
        except Exception:
            pass
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        r = round(v, 6)
        return int(r) if r == int(r) and abs(r) < 1e15 else r
    if isinstance(v, int) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            pass
    # last resort: refuse to emit bare float nan via str
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return str(v)


def frame_to_columnar(df, name: str) -> dict:
    """pandas DataFrame -> {"columns": [...], "rows": [[...]]} with day cols as ISO."""
    import pandas as pd  # noqa: F401  (imported for isna)

    cols = list(df.columns)
    out_rows = []
    # normalise day-ish columns to 'YYYY-MM-DD' strings up front
    df = df.copy()
    for c in cols:
        if c in DAY_COLS:
            try:
                df[c] = pd.to_datetime(df[c], errors="coerce", utc=False)
                df[c] = df[c].dt.strftime("%Y-%m-%d")
            except Exception:
                df[c] = df[c].astype(str)
    for tup in df.itertuples(index=False, name=None):
        out_rows.append([_jsonable(v) if not _is_na(v) else None for v in tup])
    return {"columns": cols, "rows": out_rows, "n": len(out_rows), "table": name}


def _is_na(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and not math.isfinite(v):
        return True
    try:
        import pandas as pd
        if v is pd.NaT:
            return True
        res = pd.isna(v)
        return bool(res) if isinstance(res, bool) else False
    except Exception:
        return False


def _connect_ro_or_snapshot(path: Path, log, prefix: str):
    """Open read-only; if a writer holds the lock, read a snapshot copy
    instead of waiting or (never) opening for write. Returns (con, tmp|None)."""
    import duckdb

    tmp = None
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:
        log(f"  read-only open failed ({type(exc).__name__}: {exc}); "
            f"reading a snapshot copy instead")
        tmp = Path(tempfile.mkdtemp(prefix=prefix)) / path.name
        shutil.copy2(path, tmp)
        for side in (".wal", ".tmp"):
            s = Path(str(path) + side)
            if s.exists():
                shutil.copy2(s, Path(str(tmp) + side))
        con = duckdb.connect(str(tmp), read_only=True)
    return con, tmp


TAG_EXPORT_TOP = 150  # hashtags shipped to the browser (see load_from_duckdb)


def load_from_duckdb(path: Path, log) -> tuple[dict, list[str]]:
    """Open read-only and pull every contract table that exists."""
    con, tmp = _connect_ro_or_snapshot(path, log, prefix="metrics-ro-")

    try:
        present = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        tables, found = {}, []
        for name in CONTRACT:
            if name not in present:
                continue
            if name == "tag_metrics_daily" and "alert_candidates" in present:
                # The hashtag panel ranks tags, so shipping every tag-day row
                # (~0.5M rows, ~190 MB of JSON) buys nothing. Keep the busiest
                # tags plus any tag that raised an alert.
                total = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                cols = ", ".join(f'"{c}"' for c in CONTRACT[name])
                df = con.execute(f'''
                    SELECT {cols} FROM "{name}" WHERE tag_id IN (
                        SELECT tag_id FROM "{name}" GROUP BY tag_id
                        ORDER BY SUM(V) DESC NULLS LAST LIMIT {TAG_EXPORT_TOP})
                    OR tag_id IN (SELECT tag_id FROM alert_candidates
                                  WHERE tag_id IS NOT NULL)''').fetchdf()
                log(f"  (tag_metrics_daily capped to top {TAG_EXPORT_TOP} tags "
                    f"+ alerting tags: {len(df):,} of {total:,} rows)")
            else:
                df = con.execute(f'SELECT * FROM "{name}"').fetchdf()
            tables[name] = df
            found.append(name)
            log(f"  {name:22s} {len(df):8,d} rows x {len(df.columns):2d} cols")
        extra = sorted(present - set(CONTRACT))
        if extra:
            log(f"  (non-contract tables present, ignored: {', '.join(extra)})")
        return tables, found
    finally:
        con.close()
        if tmp is not None:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def load_geo_tables(path: Path, log) -> tuple[dict, dict, str]:
    """Load the geo-ontology layer (entity/risk/theme/alerts) from its own DB.

    Returns (tables, geo_kpi, geo_mode). `tables` keys are exactly GEO_TABLES
    (only those present); `geo_kpi` is a small dict of scalar aggregates
    computed with cheap SQL (never by shipping raw entity_mentions/risk_labels/
    theme_labels rows, which run 20K-110K rows -- too large to be worth it for
    numbers a GROUP BY already gives). geo_mode is 'absent' | 'partial' | 'real'.
    """
    empty = ({}, {}, "absent")
    if not path.exists():
        log(f"{path} does not exist yet -- geo-ontology sections will show "
            f"an awaiting-data state")
        return empty

    log(f"reading {path} (read-only)")
    con, tmp = _connect_ro_or_snapshot(path, log, prefix="geo-ro-")
    try:
        present = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        tables: dict = {}
        for name in GEO_TABLES:
            if name not in present:
                continue
            if name == "entity_dim":
                # Directory ships entities seen in >=2 distinct posts -- the
                # ~22K singleton/NER-noise rows are kept in geo_kpi's totals
                # (nothing is dropped from the *count*), just not shipped row
                # by row into a UI table meant to be skimmed, not scrolled.
                df = con.execute(
                    "SELECT * FROM entity_dim WHERE distinct_posts >= 2 "
                    "ORDER BY mention_count DESC").fetchdf()
            elif name == "geo_alert_candidates":
                df = con.execute(
                    "SELECT * FROM geo_alert_candidates ORDER BY day, "
                    "dimension_type, q_value").fetchdf()
            else:
                df = con.execute(f'SELECT * FROM "{name}"').fetchdf()
            tables[name] = df
            log(f"  {name:22s} {len(df):8,d} rows x {len(df.columns):2d} cols")

        if not tables:
            return empty

        def scalar(sql, default=0):
            try:
                r = con.execute(sql).fetchone()
                return r[0] if r and r[0] is not None else default
            except Exception:
                return default

        kpi = {
            "entities_total": scalar("SELECT count(*) FROM entity_dim"),
            "entities_kept": scalar(
                "SELECT count(*) FROM entity_dim WHERE distinct_posts >= 2"),
            "entities_by_type": {
                r[0]: {"n_entities": r[1], "mentions": r[2]}
                for r in con.execute(
                    "SELECT entity_type, count(*), sum(mention_count) "
                    "FROM entity_dim GROUP BY 1").fetchall()
            },
            "mentions_total": scalar(
                "SELECT count(*) FROM entity_mentions"),
            "mentions_gazetteer_share": scalar(
                "SELECT avg((subtype_source='gazetteer')::int) "
                "FROM entity_mentions", None),
            "risk_labels_total": scalar("SELECT count(*) FROM risk_labels"),
            "theme_labels_total": scalar("SELECT count(*) FROM theme_labels"),
            "corpus_size": scalar(f"SELECT count(*) FROM {TOPIC_POST_TABLE}"),
            "posts_with_any_label": scalar(
                "SELECT count(*) FROM (SELECT post_uid FROM risk_labels "
                "UNION SELECT post_uid FROM theme_labels)"),
            "alerts_total": scalar(
                "SELECT count(*) FROM geo_alert_candidates"),
            "alerts_by_dim": {
                r[0]: r[1] for r in con.execute(
                    "SELECT dimension_type, count(*) FROM geo_alert_candidates "
                    "GROUP BY 1").fetchall()
            },
            "window_start": scalar(
                "SELECT min(day)::varchar FROM entity_metrics_daily", None),
            "window_end": scalar(
                "SELECT max(day)::varchar FROM entity_metrics_daily", None),
        }
        complete = all(t in tables and len(tables[t]) for t in GEO_TABLES)
        mode = "real" if complete else "partial"
        return tables, kpi, mode
    except Exception as exc:
        log(f"  !! could not read geo-ontology DB: {type(exc).__name__}: {exc}")
        return empty
    finally:
        con.close()
        if tmp is not None:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def check_contract(tables: dict, log) -> dict:
    """Report -- never enforce -- differences against §5."""
    report = {"missing_tables": [], "empty_tables": [], "column_diffs": {}}
    for name, cols in CONTRACT.items():
        if name not in tables:
            report["missing_tables"].append(name)
            continue
        df = tables[name]
        if len(df) == 0:
            report["empty_tables"].append(name)
        if cols is None:
            continue
        have = list(df.columns)
        missing = [c for c in cols if c not in have]
        extra = [c for c in have if c not in cols]
        if missing or extra:
            report["column_diffs"][name] = {"missing": missing, "extra": extra}
    for name in report["missing_tables"]:
        log(f"  ! contract table absent: {name}")
    for name in report["empty_tables"]:
        log(f"  ! contract table present but empty: {name}")
    for name, d in report["column_diffs"].items():
        if d["missing"]:
            log(f"  ! {name}: columns missing vs §5 -> {', '.join(d['missing'])}")
        if d["extra"]:
            log(f"  . {name}: extra columns beyond §5 -> {', '.join(d['extra'])}")
    return report


# ==========================================================================
# derived payload for the notification service
# ==========================================================================

def build_derived(tables: dict) -> dict:
    import numpy as np
    import pandas as pd

    md = tables.get("metrics_daily")
    ac = tables.get("alert_candidates")
    td = tables.get("topic_dim")
    tg = tables.get("tag_dim")

    labels = {}
    if td is not None and "topic_id" in td.columns:
        for _, r in td.iterrows():
            labels[r["topic_id"]] = (r.get("label") or r.get("topic") or r["topic_id"])
    taglab = {}
    if tg is not None and "tag_id" in tg.columns:
        for _, r in tg.iterrows():
            taglab[r["tag_id"]] = r.get("tag")

    meta = {}
    mm = tables.get("metric_meta")
    if mm is not None:
        for _, r in mm.iterrows():
            meta[r["metric_key"]] = {k: _jsonable(r[k]) for k in mm.columns}

    out = {"kpi": {}, "alerts": [], "emergence_top": [], "metric_index": sorted(meta)}

    if md is not None and len(md):
        inw = md[md["in_window"] == True] if "in_window" in md.columns else md  # noqa: E712
        out["kpi"] = {
            "posts_total": int(pd.to_numeric(md.get("V"), errors="coerce").fillna(0).sum()),
            "posts_in_window": int(pd.to_numeric(inw.get("V"), errors="coerce").fillna(0).sum()),
            "cells_total": int(len(md)),
            "cells_in_window": int(len(inw)),
            "cells_support_ok": int(md["support_ok"].fillna(False).astype(bool).sum())
            if "support_ok" in md.columns else None,
            "cells_alert_flag": int(md["alert_flag"].fillna(False).astype(bool).sum())
            if "alert_flag" in md.columns else None,
        }
        if "emergence_score" in md.columns:
            comp = [c for c in ("surprise", "accel_smooth", "novelty",
                                "days_to_threshold", "burst_state", "surprise_q",
                                "V", "support_n") if c in md.columns]
            top = md.dropna(subset=["emergence_score"]).nlargest(25, "emergence_score")
            for _, r in top.iterrows():
                rec = {
                    "day": _jsonable(r.get("day")),
                    "topic_id": r.get("topic_id"),
                    "topic_label": labels.get(r.get("topic_id")),
                    "platform": r.get("platform"),
                    "emergence_score": _jsonable(r["emergence_score"]),
                    "components": {c: _jsonable(r.get(c)) for c in comp},
                    "components_present": int(sum(
                        1 for c in ("surprise", "accel_smooth", "novelty",
                                    "days_to_threshold")
                        if c in md.columns and not _is_na(r.get(c)))),
                }
                if isinstance(rec["day"], str):
                    rec["day"] = rec["day"][:10]
                out["emergence_top"].append(rec)

    if ac is not None and len(ac):
        order = {"critical": 0, "high": 1, "medium": 2}
        acs = ac.copy()
        acs["_o"] = acs["severity"].map(lambda s: order.get(str(s), 9))
        acs = acs.sort_values(["_o", "surprise_q"], na_position="last")
        for _, r in acs.iterrows():
            mk = r.get("metric_key")
            m = meta.get(mk, {})
            d = _jsonable(r.get("day"))
            out["alerts"].append({
                "day": d[:10] if isinstance(d, str) else d,
                "severity": r.get("severity"),
                "topic_id": r.get("topic_id"),
                "topic_label": labels.get(r.get("topic_id")),
                "tag_id": r.get("tag_id"),
                "tag": taglab.get(r.get("tag_id")),
                "platform": r.get("platform"),
                "metric_key": mk,
                "metric_display_name": m.get("display_name", mk),
                "metric_formula": m.get("formula"),
                "metric_caveats": m.get("caveats"),
                "support_gate": m.get("support_gate"),
                "value": _jsonable(r.get("value")),
                "surprise_q": _jsonable(r.get("surprise_q")),
                "rationale": r.get("rationale"),
                "grain": "hashtag" if r.get("tag_id") else "topic",
            })
        out["kpi"]["alert_candidates"] = int(len(ac))
        out["kpi"]["alerts_by_severity"] = {
            str(k): int(v) for k, v in ac["severity"].value_counts().items()}
    else:
        out["kpi"].setdefault("alert_candidates", 0)
        out["kpi"]["alerts_by_severity"] = {}

    return out


JSON_NOTES = {
    "what_this_is": (
        "Every number the dashboard at UI/index.html draws, in one artifact. "
        "A consumer of these topic/platform/tag-grain alerts (derived.alerts, from "
        "metrics-v2's alert_candidates) would never disagree with the dashboard, since "
        "both draw from this same file -- but note the one notifier that actually "
        "exists (scripts/notify_poller.py) reads a different, newer export instead: "
        "UI/data/geo_alerts.json (entity/risk/theme grain, from geo_ontology.duckdb's "
        "geo_alert_candidates). Nothing consumes derived.alerts today. See "
        "docs/NOTIFICATION_POLLER_AUDIT.md §6 before building one -- decide "
        "whether it should replace, complement, or be retired in favor of the poller "
        "that already exists."),
    "contract": (
        "Table and column names are exactly the schema contract in "
        "docs/METRICS_V2_SPEC.md §5. `tables` holds them verbatim; nothing here "
        "is renamed or recomputed."),
    "encoding": (
        "`tables[name]` is columnar: {\"columns\": [...], \"rows\": [[...], ...]}. "
        "Row i maps positionally onto `columns`. SQL NULL / NaN / NaT are all "
        "JSON null. Floats are rounded to 6 decimals. Day-valued columns are "
        "'YYYY-MM-DD' strings."),
    "null_means_gated": (
        "A null in a gated metric is not a missing measurement, it is a refusal: "
        "support_ok = false means the cell did not clear "
        "baseline_days >= 14 AND baseline_mean >= 0.5 AND V_obs >= 3, so the "
        "metric was emitted as NULL rather than as a guess (spec §6.2). Do not "
        "coalesce these to zero."),
    "alerting": (
        "`derived.alerts` is the alert feed, pre-sorted by severity then q-value, "
        "with each alert's metric semantics (display name, formula, caveats, "
        "support gate) resolved in-band from metric_meta. Thresholds are "
        "Benjamini-Hochberg FDR q-values computed per day, never raw z cutoffs "
        "(spec §6.5). `severity` is evidence strength, not impact: a critical "
        "q-value on a 4-post cell is strong evidence of a small thing."),
    "key_alerts_on_topic_not_hashtag": (
        "Hashtags cover ~15% of the corpus and ~13% of Telegram, the dominant "
        "source (spec §4). Alerts keyed on hashtags would be blind to ~85% of "
        "collected material. Key on topic_id; treat tag_id as a secondary facet. "
        "Alerts carrying a tag_id are marked grain='hashtag'."),
    "hate_is_blocked": (
        "The hate_* family is UNCALIBRATED and unpublishable (spec §6.3). A "
        "war/terror corpus over-fires off-the-shelf toxicity models. Never route "
        "a notification off hate_*."),
    "reach_is_a_proxy": (
        "reach_proxy and reach_audience_norm are proxies. No follower or "
        "subscriber counts exist anywhere in the corpus."),
    "window": (
        "Time-series metrics (growth, acceleration, momentum, burst, persistence) "
        "exist only where in_window = true. Pre-window rows are corpus members "
        "(e.g. YouTube backfill reaching to 2011), not points in a live series, "
        "and must never be used for trends."),
    "emergence_is_a_rank": (
        "emergence_score is a rank-combination of four components and is "
        "therefore only comparable WITHIN a day. `derived.emergence_top` carries "
        "components_present: a score built from 2 of 4 components is weaker "
        "evidence than one built from 4."),
}


# ==========================================================================
# HTML
# ==========================================================================

def esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Doc:
    def __init__(self):
        self.parts: list[str] = []
        self.toc: list[tuple[str, str]] = []

    def raw(self, s: str):
        self.parts.append(s)

    def html(self) -> str:
        return "\n".join(self.parts)


def metric_bullets(meta: dict, keys, present) -> str:
    """One <li> per metric that actually exists, straight out of metric_meta."""
    out = []
    for k in keys:
        if k not in present:
            continue
        m = meta.get(k)
        if not m:
            out.append(f"<li><b>{esc(k)}</b> — not described in <code>metric_meta</code>; "
                       "the dashboard will not claim semantics it was not given.</li>")
            continue
        bits = [f"<b>{esc(m.get('display_name') or k)}</b> "
                f"<span class=\"gate\">{esc(m.get('formula') or '')}</span>"]
        if m.get("units"):
            bits.append(f" <span class=\"fnote\">units: {esc(m['units'])}</span>")
        if m.get("caveats"):
            bits.append(f"<div class=\"caveat\">{esc(m['caveats'])}</div>")
        if m.get("support_gate"):
            bits.append("<div class=\"fnote\">gate: "
                        f"<span class=\"gate\">{esc(m['support_gate'])}</span></div>")
        out.append("<li>" + "".join(bits) + "</li>")
    if not out:
        return ('<div class="empty">None of this section\'s metrics are present in '
                'the loaded data.</div>')
    return ('<ul style="margin:0.6rem 0 0;padding-left:1.1rem;font-size:0.8rem;'
            'color:var(--ink-2)">' + "".join(out) + "</ul>")


def section(doc: Doc, num: str, sid: str, title: str, lede: str, body: str,
            badges: str = "") -> None:
    doc.toc.append((sid, f"{num}. {title}"))
    doc.raw(f"""
<section class="sec" id="{sid}">
  <h2><span class="num">{num}</span>{esc(title)}{badges}</h2>
  <p class="lede">{lede}</p>
  {body}
</section>""")


def build_html(payload: dict, tables: dict, mode: str, source_label: str,
               contract_report: dict) -> str:
    meta = {}
    if "metric_meta" in tables:
        mm = tables["metric_meta"]
        for _, r in mm.iterrows():
            meta[r["metric_key"]] = {c: (None if _is_na(r[c]) else r[c]) for c in mm.columns}

    md_cols = set(tables["metrics_daily"].columns) if "metrics_daily" in tables else set()
    have = {t: (t in tables and len(tables[t]) > 0) for t in CONTRACT}
    have_geo = {t: (t in tables and len(tables[t]) > 0) for t in GEO_TABLES}
    gk = payload.get("geo_kpi") or {}

    css = (HERE / "assets" / "dash.css").read_text(encoding="utf-8")
    js = (HERE / "assets" / "dash.js").read_text(encoding="utf-8")
    data_json = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    # </script> can never appear inside the embedded JSON
    data_json = data_json.replace("</", "<\\/")

    win = payload.get("window", {})
    doc = Doc()

    def awaiting(table_name: str) -> str:
        return (f'<div class="empty">Awaiting <code>{esc(table_name)}</code> from the '
                f'metrics builder. The section is wired and will populate as soon as '
                f'the table appears in <code>data/metrics.duckdb</code>.</div>')

    def geo_awaiting(table_name: str) -> str:
        return (f'<div class="empty">Awaiting <code>{esc(table_name)}</code>. The section '
                f'is wired and will populate as soon as the table appears in '
                f'<code>data/geo_ontology.duckdb</code>.</div>')

    # ---------------------------------------------------------------- 1 ----
    section(doc, "1", "kpi", "Header & KPI row",
            "The five numbers an analyst should see before anything else. Everything "
            "below is scoped by the filter row above — these tiles move with it, so "
            "the headline and the charts can never disagree.",
            '<div data-panel="kpis"></div>')

    # ---------------------------------------------------------------- 2 ----
    alert_body = f"""
<div class="card">
  <p class="ct">How a candidate becomes an alert</p>
  <p class="cs">This is the logic the notification service will consume, so it is
  spelled out rather than buried. Each cell's volume is tested against a
  Negative-Binomial baseline fitted on its own trailing window, giving an
  exceedance probability. Those p-values are corrected across every supported
  cell <em>within the same day</em> by Benjamini–Hochberg, and
  <code>alert_flag</code> is <code>q &lt; 0.05</code> — a calibrated
  false-discovery rate, not an arbitrary z threshold. Cells that fail the support
  gate are never tested at all, so they cannot be flagged.</p>
  <div class="fnote">Severity bands: <span class="sev critical"><span class="ic">◆</span>critical</span>
  q &lt; 0.001 &nbsp;·&nbsp; <span class="sev high"><span class="ic">▲</span>high</span> q &lt; 0.01
  &nbsp;·&nbsp; <span class="sev medium"><span class="ic">●</span>medium</span> q &lt; 0.05.
  Severity is <b>evidence strength, not impact</b> — a critical q-value on a four-post
  cell is strong evidence of a small thing. Colour never carries the severity alone:
  every chip pairs an icon with the word.</div>
</div>
<div class="card">
  <p class="ct">Alert candidates per day</p>
  <div class="plot" data-panel="alerts_day"></div>
</div>
<div class="card">
  <p class="ct">Alert candidates</p>
  <p class="cs">Sorted by severity, then by q-value. The rationale column is the
  human-readable trace of why the cell fired — it is carried in
  <code>alert_candidates.rationale</code> so the notification payload can quote it
  verbatim.</p>
  {'<div data-panel="alerts_table"></div>' if have['alert_candidates'] else awaiting('alert_candidates')}
</div>
{metric_bullets(meta, ['surprise', 'surprise_p', 'surprise_q', 'alert_flag', 'severity'], md_cols | {'severity'})}
"""
    section(doc, "2", "alerts", "Alerts (FDR-gated)",
            "The highest-value section: which cells are behaving unusually enough to "
            "act on, and the exact evidence behind each one.",
            alert_body,
            badges='<span class="badge">feeds the notification service</span>')

    # ---------------------------------------------------------------- 3 ----
    accel_body = f"""
<div class="card">
  <p class="ct">Why each cell flagged — the components, not just the composite</p>
  <p class="cs">The composite <code>emergence_score</code> is a rank-combination, so
  a high score can come from very different evidence. Each panel below breaks a
  top-ranked cell into its four inputs, and shows explicitly which of them were
  <b>NULL</b> — a score built from two components is weaker evidence than one built
  from four, and the composite alone will not tell you which you have.</p>
  <div class="plot" data-panel="emg_components"></div>
</div>
<div class="card">
  <p class="ct">Smoothed acceleration by topic</p>
  <p class="cs">One panel per topic: nine topics cannot be separated by hue in a
  black/white/gray/gold palette, so identity is carried by position instead. The
  gold ribbon under each axis marks days where the burst detector was in its burst
  state — an ordered state channel, not a fourth series.</p>
  <div class="plot" data-panel="accel_facets"></div>
</div>
<div class="grid g2">
  <div class="card">
    <p class="ct">Semantic novelty by topic</p>
    <p class="cs">A topic can stay flat in volume while completely changing in
    content. This is the metric that sees that.</p>
    <div class="plot" data-panel="novelty_facets"></div>
  </div>
  <div class="card">
    <p class="ct">Days from first appearance to 5 posts/day</p>
    <p class="cs">Speed of emergence from zero. Lower is faster.</p>
    <div class="plot" data-panel="dtt_bars"></div>
  </div>
</div>
{metric_bullets(meta, ['accel', 'accel_smooth', 'burst_state', 'burst_intensity', 'novelty', 'days_to_threshold', 'emergence_score'], md_cols)}
"""
    section(doc, "3", "accel", "Acceleration & emergence",
            "“How fast is a scenario popping up” is not one quantity — it bundles "
            "curvature, a burst state, emergence from zero, and semantic novelty. All "
            "four are shown separately so an analyst can always ask <em>why</em> "
            "something flagged.",
            accel_body,
            badges='<span class="badge">priority metric family</span>')

    # ---------------------------------------------------------------- 4 ----
    section(doc, "4", "bursts", "Burst episodes",
            "Detected episodes as intervals on a shared time axis: when each started, "
            "when it peaked, when it ended, how steeply it arrived and how fast it "
            "decayed.",
            f"""
<div class="card">
  <p class="ct">Episode timeline</p>
  <p class="cs">Bar = episode extent, dot = peak day. Hover any bar for onset slope,
  inflection day, decay λ, half-life and the decay fit's r². Post-peak exponential
  decay is scoped to <em>detected episodes</em> rather than whole series, which is why
  it fits here where the v1 whole-series version accepted only 6 of 39 cells.</p>
  <div class="plot" data-panel="bursts"></div>
</div>
{metric_bullets(meta, ['onset_slope', 'half_life_days_episode', 'burst_state', 'burst_intensity'], set(meta))}
""")

    # ---------------------------------------------------------------- 5 ----
    section(doc, "5", "volume", "Volume & growth",
            "Volume, the effort-normalised rate that removes the collector ramp, and "
            "the growth family derived from it.",
            f"""
<div class="card">
  <p class="ct">Volume rate per platform, with collection effort beneath</p>
  <p class="cs">Effort is plotted as its own strip with its own baseline, never as a
  second y-axis on the same plot. A rise in raw <code>V</code> that is matched by a
  rise in effort is the scraper, not the world — this layout is what makes that
  visible instead of hiding it inside a ratio.</p>
  <div class="plot" data-panel="vol_facets"></div>
</div>
<div class="grid g2">
  <div class="card">
    <p class="ct">Growth by topic, most recent supported day</p>
    <p class="cs">Diverging scale: gold for accelerating, clay for decelerating,
    neutral gray at zero — never a hue at the midpoint.</p>
    <div class="plot" data-panel="growth_div"></div>
  </div>
  <div class="card">
    <p class="ct">Momentum and 7-day growth by topic</p>
    <p class="cs">Two series only, so gold carries the focal measure and gray the
    reference. Both are in the same units (log-rate), so they share one axis.</p>
    <div class="plot" data-panel="momentum_facets"></div>
  </div>
</div>
{metric_bullets(meta, ['V', 'V_rate', 'effort', 'growth', 'growth_share', 'growth_7d', 'momentum'], md_cols | {'effort'})}
""")

    # ---------------------------------------------------------------- 6 ----
    section(doc, "6", "intensity", "Intensity & surprise",
            "Where and when volume exceeded its own baseline by more than chance "
            "explains — the cross-section of the whole panel in one view.",
            f"""
<div class="card">
  <p class="ct">Topic × day surprise</p>
  <p class="cs">A single gold ramp, light→dark: one hue, so the reader sees magnitude
  and nothing else. Hatched cells are <b>not zero</b> — they are cells whose support
  gate was not met, where the metric was emitted as NULL rather than as a guess. A
  white outline marks cells that cleared BH q &lt; 0.05.</p>
  <div data-panel="intensity_heat"></div>
</div>
{metric_bullets(meta, ['surprise', 'surprise_q', 'support_n', 'support_ok'], md_cols)}
""")

    # ---------------------------------------------------------------- 7 ----
    section(doc, "7", "persistence", "Persistence",
            "Whether a topic's attention sticks or dissipates, estimated from the "
            "whole series with a standard error instead of by discarding cells whose "
            "curve fit was weak.",
            f"""
<div class="card">
  <p class="ct">AR(1) φ with ± 1 standard error</p>
  <p class="cs">Gold dot = the point estimate, gray bar = ± 1 SE. Cells whose
  Ljung–Box p ≥ 0.05 are drawn in gray: there is no significant autocorrelation to
  model, so their φ and half-life describe noise. φ outside (0, 1) leaves the
  half-life genuinely undefined, and it is shown as “—” rather than filled in.</p>
  {'<div class="plot" data-panel="persistence"></div>' if have['persistence'] else awaiting('persistence')}
</div>
{metric_bullets(meta, ['ar1_phi', 'phi_se', 'half_life_days', 'volatility', 'ljung_box_q', 'ljung_box_p'], set(meta))}
""")

    # ---------------------------------------------------------------- 8 ----
    section(doc, "8", "tone", "Tone",
            "Sentiment polarity per cell, published as the empirical-Bayes shrunk "
            "value with its support count always visible.",
            f"""
<div class="grid g2">
  <div class="card">
    <p class="ct">Tone by topic × platform</p>
    <p class="cs">Diverging: clay for negative, gold for positive, a neutral
    <b>gray</b> midpoint. Hatched cells had no tone-scored post at all.</p>
    <div data-panel="tone_heat"></div>
  </div>
  <div class="card">
    <p class="ct">Shrinkage: raw mean vs published EB value</p>
    <p class="cs">The connector shows how far empirical Bayes moved each topic. A
    long connector means the raw mean rested on very little evidence — the support
    count on the right is the number to read first.</p>
    <div class="plot" data-panel="tone_shrink"></div>
  </div>
</div>
{metric_bullets(meta, ['tone_mean', 'tone_eb', 'tone_n'], md_cols)}
""")

    # ---------------------------------------------------------------- 9 ----
    hate_caveat = (meta.get("hate_eb") or {}).get("caveats") or (
        "A war/terror corpus over-fires off-the-shelf toxicity models.")
    hate_gate = (meta.get("hate_eb") or {}).get("support_gate") or "BLOCKED"
    section(doc, "9", "hate", "Hate / toxicity",
            "Present in the schema, deliberately <b>not</b> presented as a finding.",
            f"""
<div class="warnbox">
  <div class="hd"><span aria-hidden="true">⚠</span> Uncalibrated — not publishable</div>
  <p style="margin:0 0 0.4rem">{esc(hate_caveat)}</p>
  <p style="margin:0">Per spec §6.3 this family stays gated until
  <code>reports/calibration_sample.csv</code> is hand-labelled and agreement is
  measured. Until then: <b>do not quote these numbers, do not rank topics by them,
  and do not route a notification off them.</b> Gate:
  <span class="gate">{esc(hate_gate)}</span></p>
</div>
<div class="card">
  <p class="ct">Uncalibrated values <span class="badge blocked">⚠ uncalibrated</span></p>
  <p class="cs">Collapsed by default and rendered in gray rather than gold, because
  gold is this dashboard's signal for “trust this”. The numbers are shown at all only
  so the calibration work can be scoped.</p>
  <div data-panel="hate_gated"></div>
</div>
{metric_bullets(meta, ['hate_mean', 'hate_eb', 'hate_prevalence', 'hate_calibrated'], md_cols | {'hate_calibrated'})}
""",
            badges='<span class="badge crit">⚠ gated</span>')

    # --------------------------------------------------------------- 10 ----
    section(doc, "10", "virality", "Virality & reach",
            "Engagement expressed as an age-conditional percentile, because a "
            "parametric age normalisation did not fit — and reach labelled for what "
            "it actually is.",
            f"""
<div class="card">
  <p class="ct">Virality percentile distribution per platform</p>
  <p class="cs">Scrape lag ranges from 0.6 days to 150 days across platforms, so raw
  engagement comparisons measure observation delay, not virality. A percentile
  within (platform × age band) is distribution-free and age-honest.</p>
  <div class="plot" data-panel="virality_facets"></div>
</div>
<div class="card">
  <p class="ct">Reach <span class="badge">proxy</span></p>
  <p class="cs"><b>No follower or subscriber counts exist anywhere in the corpus.</b>
  This is a proxy — views where available, otherwise a weighted engagement composite.
  For Telegram only, a channel's median views gives a defensible audience-size
  denominator (<code>reach_audience_norm</code>), which is still a proxy. Never
  present either as audience size.</p>
  <div class="plot" data-panel="reach_bars"></div>
</div>
{metric_bullets(meta, ['virality_pct', 'reach_proxy', 'reach_audience_norm'], md_cols)}
""")

    # --------------------------------------------------------------- 11 ----
    section(doc, "11", "authors", "Author diversity & coordination",
            "Whether a topic's volume comes from many voices or from one account "
            "repeating itself.",
            f"""
<div class="card">
  <p class="ct">Author dispersion against author count</p>
  <p class="cs">Entropy rises with volume because it is bounded by log(n_authors);
  1 − HHI does not, which is why both are kept. Coordination-flagged cells are marked
  with a red <b>✕</b> and named in the legend — never by colour alone.</p>
  <div class="plot" data-panel="authors_scatter"></div>
</div>
<div class="card">
  <p class="ct">Author entropy over time, per platform</p>
  <div class="plot" data-panel="entropy_facets"></div>
</div>
{metric_bullets(meta, ['author_entropy', 'author_1_hhi', 'n_authors', 'coordination_flag'], md_cols)}
""")

    # --------------------------------------------------------------- 12 ----
    section(doc, "12", "readability", "Readability",
            "The Flesch mean, and — inseparably — the share of posts that were long "
            "enough to score at all.",
            f"""
<div class="card">
  <p class="ct">Readability mean and its coverage</p>
  <p class="cs">YouTube comments have a median length of 59 characters with 36% under
  40, which puts most of the corpus below the validity floor for any readability
  formula. The mean therefore describes a minority of posts, and the coverage meter
  is what tells you how small that minority is. A coverage figure below 20% is
  drawn in gray, not gold.</p>
  <div data-panel="readability"></div>
</div>
{metric_bullets(meta, ['readability_mean', 'readability_coverage'], md_cols)}
""")

    # --------------------------------------------------------------- 13 ----
    section(doc, "13", "ontology", "Ontology",
            "The entity every per-topic metric is about. Ontology was promoted from "
            "an output to a dependency: <code>keyword</code> is NULL for the whole of "
            "Telegram, so without derived clusters two thirds of the corpus has no "
            "answer to “a signal about <em>what</em>?”.",
            f"""
{'<div data-panel="topic_cards"></div>' if have['topic_dim'] else awaiting('topic_dim')}
<div class="caveat"><b>Unassigned is shown, not hidden.</b> Posts the clustering pass
could not place are kept as an explicit topic so coverage gaps stay visible instead of
silently vanishing from every denominator.</div>
""")

    # --------------------------------------------------------------- 14 ----
    tag_cov = ""
    if have["tag_dim"]:
        tg = tables["tag_dim"]
        tag_cov = f"{len(tg):,} distinct tags are in <code>tag_dim</code>. "
    section(doc, "14", "hashtags", "Hashtags",
            "Built, because hashtag alerting genuinely works where hashtags exist — "
            "and framed, because most of this corpus has none.",
            f"""
<div class="warnbox">
  <div class="hd"><span aria-hidden="true">⚠</span> Coverage warning — spec §4</div>
  <p style="margin:0 0 0.4rem">The <code>hashtags</code> field is populated for
  LinkedIn (502/924) and Instagram (237/337) and is <b>empty for Telegram, YouTube,
  Reddit and Facebook</b>. Mining inline <code>#tags</code> from post text adds only
  Telegram 1,883/14,595 (12.9%) and Reddit 66/2,107 (3.1%).</p>
  <p style="margin:0 0 0.4rem"><b>Hashtags cover roughly 15% of the corpus and ~13%
  of Telegram, the dominant source. A hashtag-keyed alerting system would be blind to
  about 85% of collected material.</b></p>
  <p style="margin:0">{tag_cov}<b>Key alerts on <code>topic_id</code></b>, which covers
  79% of posts with the remainder explicitly <code>unassigned</code>; treat hashtag
  and keyword as secondary facets. Alerts in
  <code>UI/data/metrics.json</code> that came from a hashtag are tagged
  <code>grain: "hashtag"</code> so a consumer can weight them accordingly.</p>
</div>
<div class="card">
  <p class="ct">Top tags by volume in view</p>
  {'<div class="plot" data-panel="tags"></div>' if have['tag_metrics_daily'] else awaiting('tag_metrics_daily')}
</div>
""",
            badges='<span class="badge crit">⚠ ~15% coverage</span>')

    # --------------------------------------------------------------- 15 ----
    cr = contract_report
    mismatch_html = ""
    if cr["missing_tables"] or cr["empty_tables"] or cr["column_diffs"]:
        rows = []
        for t in cr["missing_tables"]:
            rows.append(f"<li><b>{esc(t)}</b> — contract table absent from the source. "
                        "Sections that need it show an explicit awaiting-data state.</li>")
        for t in cr["empty_tables"]:
            rows.append(f"<li><b>{esc(t)}</b> — present but zero rows.</li>")
        for t, d in cr["column_diffs"].items():
            if d["missing"]:
                rows.append(f"<li><b>{esc(t)}</b> — columns in §5 but not in the data: "
                            f"<code>{esc(', '.join(d['missing']))}</code></li>")
            if d["extra"]:
                rows.append(f"<li><b>{esc(t)}</b> — columns beyond §5 (carried through "
                            f"unchanged): <code>{esc(', '.join(d['extra']))}</code></li>")
        mismatch_html = (
            '<div class="warnbox"><div class="hd"><span aria-hidden="true">⚠</span> '
            'Contract differences detected at build time</div><ul style="margin:0;'
            'padding-left:1.1rem">' + "".join(rows) + "</ul></div>")
    else:
        mismatch_html = ('<div class="card"><p class="ct">Contract check</p>'
                         '<p class="cs">Every table and column in '
                         f'<code>{esc(SPEC)}</code> §5 was found in the source, with no '
                         'unexpected additions.</p></div>')

    section(doc, "15", "methodology", "Methodology & data quality",
            "What every number on this page is allowed to mean, and where it stops.",
            f"""
<div class="grid g2">
  <div class="card">
    <p class="ct">Data quality in the current selection</p>
    <div data-panel="quality"></div>
  </div>
  <div class="card">
    <p class="ct">Run metadata</p>
    <div data-panel="run_meta"></div>
  </div>
</div>
{mismatch_html}
<div class="card">
  <p class="ct">Known caveats carried through the whole dashboard</p>
  <ul style="margin:0.2rem 0 0;padding-left:1.1rem;font-size:0.82rem;color:var(--ink-2)">
    <li><b>The corpus is live.</b> It ingests while it is read, so any two queries run
    separately disagree. Every extract runs inside one <code>REPEATABLE READ</code>
    snapshot; production Postgres is read-only and nothing is ever written back.</li>
    <li><b>The collector is a confounder.</b> Telegram went 422 → 1,393 posts/day
    while active channels went 18 → 24. All trend metrics are computed on the
    effort-normalised log rate for exactly this reason; raw counts are kept beside
    them for transparency, not for trends.</li>
    <li><b>Duplicates were a prerequisite, not hygiene.</b> Telegram had 37% exact
    duplicate <code>post_url</code>s (Reddit 36%, Instagram 43% duplicate text), which
    inflated every Telegram volume figure by roughly 1.6× before dedup.</li>
    <li><b>Sparsity is handled in the model, not by clipping.</b> v1's robust-z reached
    |z| = 143 on cells of ≤ 2 posts because MAD collapsed toward zero. v2 uses a
    Negative-Binomial exceedance probability and a support gate, so a single post
    against a small baseline scores as the non-event it is.</li>
    <li><b>Absent is not zero.</b> Reddit has no engagement fields at all; Facebook has
    genuine zero likes on ~70% of posts. Collapsing those two into <code>0</code> would
    fabricate a finding, so engagement metrics are NULL where the field does not
    exist.</li>
    <li><b>Half-life is aggregate-level.</b> Rows are updated in place and history is
    overwritten, so every post is observed exactly once. Per-post engagement decay is
    not measurable from a single snapshot; an append-only
    <code>engagement_snapshots</code> table was seeded so the honest version becomes
    available later.</li>
    <li><b>The corpus is multilingual.</b> Telegram is 62% English, then ru/zh-cn/bn/uk/hi
    plus 2,626 <code>unknown</code>. English-only NLP would silently mishandle ~38%,
    so tone and toxicity use local multilingual models — and nothing leaves the
    network.</li>
  </ul>
</div>
<div class="card">
  <p class="ct">Every metric, from <code>metric_meta</code></p>
  <p class="cs">Formula, units, grain, direction, support gate and caveats — read from
  the data, not hardcoded here. If the metrics builder changes a definition, this
  section changes with it.</p>
  <div data-panel="meta_table"></div>
</div>
""")

    # ================================================================ geo ==
    # Geo-ontology layer: entity/risk/theme/timeline, built in
    # data/geo_ontology.duckdb + UI/data/geo_alerts.json, documented in
    # docs/GEOPOLITICAL_ONTOLOGY_SPEC.md. A separate build from the metrics v2
    # contract above, so the sections below use `have_geo`/`geo_awaiting`
    # rather than `have`/`awaiting`, and never touch `mode`/`fixture`.

    ent_type_rows = "".join(
        f"<li><b>{esc(t)}</b>: {d.get('n_entities', 0):,} entities, "
        f"{d.get('mentions', 0):,} mentions</li>"
        for t, d in sorted((gk.get("entities_by_type") or {}).items())
    ) or "<li>no entities loaded</li>"
    gaz_share = gk.get("mentions_gazetteer_share")
    gaz_pct = f"{gaz_share * 100:.1f}%" if isinstance(gaz_share, (int, float)) else "—"

    entity_body = f"""
<div class="card">
  <p class="ct">What's in the directory</p>
  <p class="cs">{gk.get('entities_total', 0):,} distinct entities were resolved from
  {gk.get('mentions_total', 0):,} NER + gazetteer mentions across the corpus
  (model: <code>Babelscape/wikineural-multilingual-ner</code>, hybrid with a
  {esc(str(104))}-surface-form port/border/pipeline/railway gazetteer). Only
  {gaz_pct} of mentions matched the gazetteer directly — the rest are model NER,
  kept even when unresolved (never dropped). The directory below ships entities
  seen in <b>&ge;2 distinct posts</b> ({gk.get('entities_kept', 0):,} of
  {gk.get('entities_total', 0):,}); singleton mentions are counted in the totals
  above but not rendered row-by-row.</p>
  <ul style="margin:0.4rem 0 0;padding-left:1.1rem;font-size:0.8rem;color:var(--ink-2)">
    {ent_type_rows}
  </ul>
</div>
<div class="caveat">
  <b>Verification status: PARTIAL / precision not measured.</b> The entity-extraction
  Implement stage finished after the independent verification pass had already run out
  of time waiting on it (it was still mid-NER at check time), so no stratified sample
  of real <code>entity_mentions</code> rows was read against source text — there is no
  measured precision figure for this table, only code-inspection risks: (1) several
  gazetteer surface forms are bare place names also used generically —
  <code>Djibouti</code>, <code>Colombo</code>, <code>Gwadar</code> — and get forced
  <code>subtype="port"</code> at <code>subtype_confidence=1.0</code> regardless of
  context, a likely systematic false-positive source; (2) the script's own "~84%
  correct" figure comes from a 216-post prototype sample, not this 20,347-post run;
  (3) <code>MAX_CHARS=1000</code> truncation silently misses entities later in long
  posts. Treat this directory as extracted-but-unaudited, not as verified ground truth.
</div>
{('<div class="card"><p class="ct">Entity taxonomy &amp; top mentions, per type</p>'
  '<p class="cs">Every subtype the schema defines &mdash; Political Leader, Military '
  'Leader, Diplomat, Business Executive (people); Ministry, State-Owned Enterprise, '
  'Armed Group, International Organization, Business/Corporate Org (organisations); '
  'Port, Border Town, Economic Zone, Pipeline, Railway (locations) &mdash; shown even '
  'where a slot has zero instances so far, dimmed rather than hidden. Click a populated '
  'card to jump to that type\'s top-mentions chart below.</p>'
  '<div data-panel="entity_top_bars"></div></div>') if have_geo['entity_dim'] else geo_awaiting('entity_dim')}
<div class="card">
  <p class="ct">Entity directory</p>
  <p class="cs">Canonical name, resolved type/subtype, how the subtype was decided
  (gazetteer vs. zero-shot vs. unresolved), and mention/post/recency counts. Search
  filters live across the full directory, not just the visible page.</p>
  {'<div data-panel="entity_search"></div><div data-panel="entity_directory_table"></div>' if have_geo['entity_dim'] else geo_awaiting('entity_dim')}
</div>
"""
    section(doc, "16", "entities", "Entity directory",
            "Who and what this corpus is actually about: people, organisations and "
            "places resolved from free text, kept even when a subtype can't be "
            "determined — unresolved is a category, not a silent drop.",
            entity_body,
            badges='<span class="badge gray">verification: partial (precision unmeasured)</span>')

    # ---------------------------------------------------------------- 17 ----
    posts_labelled = gk.get("posts_with_any_label", 0)
    corpus_size = gk.get("corpus_size", 0)
    label_cov = (posts_labelled / corpus_size) if corpus_size else None
    label_cov_s = f"{label_cov * 100:.1f}%" if label_cov is not None else "—"

    risk_theme_body = f"""
<div class="warnbox">
  <div class="hd"><span aria-hidden="true">⚠</span> Independent verification: UNSOUND —
  read the shape, not the label boundaries, as ground truth</div>
  <p style="margin:0 0 0.4rem">Method is <code>embedding_similarity_bge_m3_zeroshot</code>:
  one flat cosine-similarity threshold (0.45) applied uniformly across all 9 risk and
  12 theme categories, with no per-category calibration. A stratified 42-post sample
  (1 confident + 1 gray-zone example per category, hand-read against source text) found
  <b>precision 29% strict / 45% lenient</b> overall (43% / 62% on non-gray_zone rows,
  14% / 24% on <code>gray_zone=true</code> rows — the flag is doing real, if crude,
  work). A separate ~20-post recall spot-check via keyword search (sanctions,
  insurgent, pipeline, belt-and-road, refugee) found clean misses: e.g. a Kirill
  Dmitriev quote that EU sanctions "do not work, cost &gt;€3T" scored zero risk and
  zero theme tags; a Yemeni-missile-strike-on-a-named-pipeline post got the right risk
  tag but zero theme tags (missing <code>energy_security</code>).</p>
  <p style="margin:0">Only {label_cov_s} of the corpus ({posts_labelled:,} of
  {corpus_size:,} posts) carries any risk or theme label at all, and
  <code>gray_zone=true</code> on ~80–85% of all assigned labels. <b>Do not treat
  <code>risk_labels</code>/<code>theme_labels</code> as ground truth without
  per-category threshold re-tuning</b> — filter to <code>gray_zone=false</code> at
  minimum, and even that subset is only ~45–62% precise on lenient scoring.</p>
</div>
<div class="card">
  <p class="ct">Risk assessment — 9 categories</p>
  <p class="cs">Domestic Instability, Policy Shifts, Project Delays, Sanctions, Border
  Conflicts, Insurgencies, Shipping Disruptions, Pipeline Vulnerabilities, International
  Pressure. Card value is in-window post volume; hover for the latest surprise score and
  active-alert count. Click a card to jump to its row in the heatmap below.</p>
  {'<div data-panel="risk_overview"></div>' if have_geo['risk_metrics_daily'] else geo_awaiting('risk_metrics_daily')}
</div>
<div class="card">
  <p class="ct">Risk-category signal, by day</p>
  <p class="cs">Mean NB-surprise per category × day (same statistic as the topic
  intensity heatmap in §6), ringed where the cell cleared BH q &lt; 0.05, hatched
  where the support gate wasn't met. This shows <em>when volume moved</em> for a
  label — it does not vouch for the label's precision above.</p>
  {'<div data-panel="risk_heat"></div>' if have_geo['risk_metrics_daily'] else geo_awaiting('risk_metrics_daily')}
</div>
<div class="card">
  <p class="ct">Thematic &amp; relational dimensions — 12 categories</p>
  <p class="cs">Political Relations, Economic Relations, Belt and Road Initiative (BRI),
  Energy Security, Maritime Security, Maritime Dimensions, Security &amp; Military
  Relations, Border Issues, Internal Conflict &amp; China's Role, Technology &amp;
  Digital Influence, Humanitarian Dimension, International Relations. Click a card to
  jump to its trend panel below.</p>
  {'<div data-panel="theme_overview"></div>' if have_geo['theme_metrics_daily'] else geo_awaiting('theme_metrics_daily')}
</div>
<div class="card">
  <p class="ct">Theme prevalence over time</p>
  <p class="cs">Share of that day's labelled posts carrying each theme
  (<code>share_V</code>), one small panel per theme so 12 categories don't collapse
  onto indistinguishable hues. The gold ribbon marks days the burst detector flagged.</p>
  {'<div data-panel="theme_facets"></div>' if have_geo['theme_metrics_daily'] else geo_awaiting('theme_metrics_daily')}
</div>
"""
    section(doc, "17", "risktheme", "Risk categories & thematic dimensions",
            "9 risk categories and 12 thematic dimensions, multi-label per post via "
            "zero-shot embedding similarity to a category description — shown here, "
            "flagged here, because the fix is re-tuning, not silence.",
            risk_theme_body,
            badges='<span class="badge crit">⚠ verification: UNSOUND</span>')

    # ---------------------------------------------------------------- 18 ----
    abd = gk.get("alerts_by_dim") or {}
    timeline_body = f"""
<div class="card">
  <p class="ct">Same alerting logic as §2, extended to three more grains</p>
  <p class="cs">Entity, risk-category and theme-category daily volumes are each fit to
  their own trailing Negative-Binomial baseline (<code>scripts/lib/stats_v2.py</code>,
  reused rather than reimplemented), giving a per-cell exceedance p-value. Those are
  Benjamini–Hochberg corrected <em>within each dimension family, per day</em>
  (<code>fdr_scope="day"</code>) — entity/risk/theme each get their own correction, so
  a busy news day moving many correlated series doesn't cannibalise the correction
  budget of an unrelated family. <code>alert_flag</code> is q &lt; 0.05, exactly as in
  §2. Independently re-verified: 46/46 <code>stats_v2</code> self-checks pass, every
  stored q-value reproduces from the raw baseline fits with 0 mismatches across
  10,762 fitted cells, and every one of the {gk.get('alerts_total', 0):,} rows below
  matches its source <code>*_metrics_daily.alert_flag</code> count exactly
  ({abd.get('entity', 0)} entity, {abd.get('risk', 0)} risk, {abd.get('theme', 0)} theme).</p>
</div>
<div class="card">
  <p class="ct">Timeline — where and when each grain crossed its baseline</p>
  <p class="cs">One swimlane per dimension: all 9 risk categories, all 12 themes, and
  the top entities by alert count. Each dot is one FDR-gated day; colour + icon carry
  severity (never colour alone), matching the §2 severity legend. Hover any dot for the
  rationale string the alert actually fired on.</p>
  {'<div data-panel="geo_timeline"></div>' if have_geo['geo_alert_candidates'] else geo_awaiting('geo_alert_candidates')}
</div>
<div class="card">
  <p class="ct">Alert candidates — entity / risk / theme</p>
  <p class="cs">Sorted by severity, then q-value. Same shape as the §2 alert table,
  with a <code>dimension_type</code> column instead of an implicit topic grain.</p>
  {'<div data-panel="geo_alerts_table"></div>' if have_geo['geo_alert_candidates'] else geo_awaiting('geo_alert_candidates')}
</div>
"""
    section(doc, "18", "geotimeline", "Timeline & alerts — entity / risk / theme",
            "“Timeline Analysis” applied to the geo-ontology dimensions: the "
            "same FDR-gated surprise detector as §2, run per entity, per risk category "
            "and per theme category.",
            timeline_body,
            badges='<span class="badge">verification: SOUND</span>')

    # ---------------------------------------------------------------- 19 ----
    verdict_rows = [
        ("Topic clustering v2", "PARTIAL",
         "3 → 11 clusters, largest-cluster share 85–94% → 23–29% under every "
         "denominator checked — a real, reproducible fix (topic_dim_v2/post_topic_v2 "
         "reconcile to the row). But the single largest cluster (t010, 25.7% of the "
         "whole corpus) is still an incoherent grab-bag under direct text sampling — "
         "the old failure mode, shrunk, not eliminated."),
        ("Entity extraction", "PARTIAL / unmeasured",
         "20,347 posts NER'd, 110,983 mentions, 29,136 resolved entities — the "
         "Implement stage finished, but the verification pass ran out of time waiting "
         "on it and never read a single real mention against source text. No precision "
         "figure exists for this table yet; code-review-only risks are noted in §16."),
        ("Risk / theme labels", "UNSOUND",
         "Flat 0.45 cosine threshold across all 21 categories. Measured precision "
         "29% strict / 45% lenient; confirmed recall misses on unambiguous, "
         "textbook-clear posts. Needs per-category threshold calibration or a "
         "stronger classifier before being trusted as ground truth. Detail in §17."),
        ("Timeline metrics (entity/risk/theme)", "SOUND",
         "46/46 stats_v2 self-checks pass; independent recomputation of every NB "
         "baseline, p-value, BH q-value and alert-candidate row matched stored values "
         "with 0 mismatches. No correctness defects found. Detail in §18."),
    ]
    verdict_html = "".join(
        f'<div><dt>{esc(name)}</dt><dd><span class="sev {"critical" if v == "UNSOUND" else ("medium" if v.startswith("PARTIAL") else "high")}">'
        f'<span class="ic">{"◆" if v == "UNSOUND" else ("●" if v.startswith("PARTIAL") else "▲")}</span>{esc(v)}</span>'
        f'<div class="fnote" style="margin-top:0.25rem">{esc(detail)}</div></dd></div>'
        for name, v, detail in verdict_rows
    )
    section(doc, "19", "geoverify", "Geo-ontology — verification summary",
            "Every subsystem built for this layer, and what an independent verification "
            "pass found — including the parts that are not fully sound. Full record: "
            f"<code>{esc(GEO_SPEC)}</code>.",
            f'<div class="card"><dl class="kv">{verdict_html}</dl></div>')

    section(doc, "20", "livener", "Live NER",
            "Gemma domain routing and entity extraction over incoming posts, polled from the "
            "server while this tab is visible. New posts are routed first (priority), then the backlog.",
            '''<div class="card" id="live-ner" data-state="idle">
  <div class="ln-prov"><span class="badge gray">provisional: routing/entity accuracy not yet judged</span>
    <span class="fnote" id="ln-updated"></span></div>
  <div class="ln-note empty" id="ln-note" hidden></div>
  <div id="ln-content">
    <div class="ln-strip" id="ln-strip"></div>
    <div class="ln-cols">
      <div><div class="ct">Domains (by routed posts)</div><div id="ln-domains"></div></div>
      <div><div class="ct">Top entities (24h)</div><ol class="ln-top" id="ln-top"></ol></div>
    </div>
    <div class="ct" style="margin-top:1rem">Recent (25 newest)</div>
    <ul class="ln-feed" id="ln-feed"></ul>
  </div>
</div>''')

    toc = "".join(f'<a href="#{sid}">{esc(label)}</a>' for sid, label in doc.toc)
    mode_badge = ('<span class="badge">real data</span>' if mode.startswith("real")
                  else '<span class="badge crit">⚠ fixture data — not real</span>')
    partial = ('<span class="badge gray">partial</span>' if "partial" in mode else "")

    fixture_banner = ""
    if not mode.startswith("real"):
        fixture_banner = """
<div class="warnbox">
  <div class="hd"><span aria-hidden="true">⚠</span> Fixture mode — these are not real measurements</div>
  <p style="margin:0"><code>data/metrics.duckdb</code> was not found, so this page was
  built from <code>UI/fixture.py</code>: a seeded, contract-exact stand-in that exists
  so the dashboard can be built and verified before the metrics builder finishes.
  Every number below is synthetic. Re-run
  <code>python UI/build_dashboard.py</code> once the database exists and this banner
  disappears.</p>
</div>"""

    basic_view_html = f"""
<div class="basic-wrap">
  <div class="basic-updated" data-panel="basic_updated"></div>
  <div class="kpi-row" data-panel="basic_kpis"></div>
  <div data-panel="basic_attention"></div>
  <div class="basic-2col">
    <div class="basic-panel">
      <p class="bp-t">Risk assessment</p>
      <div data-panel="basic_risk_strip"></div>
    </div>
    <div class="basic-panel">
      <p class="bp-t">Themes at a glance</p>
      <div data-panel="basic_theme_bars"></div>
    </div>
  </div>
  <div class="basic-2col">
    <div class="basic-panel">
      <p class="bp-t">Activity over time</p>
      <div data-panel="basic_volume_trend"></div>
    </div>
    <div class="basic-panel">
      <p class="bp-t">Who's in the news</p>
      <div data-panel="basic_top_entities"></div>
    </div>
  </div>
  <div class="basic-panel">
    <p class="bp-t">Recommended for review</p>
    <div data-panel="basic_alerts_list"></div>
  </div>
  <p class="fnote" style="margin-top:1.1rem;text-align:center">
    Want the full statistical detail, verification notes, and raw tables?
    <a href="#" id="basic-to-advanced-link">Switch to the Detailed view →</a>
  </p>
</div>
"""

    generated = payload.get("generated_at", "")
    # Default: dynamic shell — HTML/JS structure is fixed; chart data loads from
    # metrics.json (snapshot, not live DB). Filters re-render client-side.
    # --static-embed still inlines the full JSON for offline file:// use.
    embed = payload.get("_embed_data", False)
    if embed:
        data_boot = f"<script>window.__METRICS__ = {data_json}; window.__METRICS_URL__ = null;</script>"
        script_block = f"<script>\n{js}\n</script>"
    else:
        data_boot = (
            "<script>"
            "window.__METRICS__ = null;"
            "window.__METRICS_URL__ = 'data/metrics.json';"
            "</script>"
        )
        # Prefer external assets so UI code is not a 4MB hard-coded blob.
        script_block = (
            '<link rel="stylesheet" href="assets/dash.css">'
            # css also inlined below for first paint; external for cache
            f"<style>\n{css}\n</style>"
            f"{data_boot}"
            '<script src="assets/dash.js"></script>'
            '<script src="assets/live.js"></script>'
        )
        # When not embedding, avoid double CSS — use external only
        return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Social Signal Monitor</title>
<link rel="stylesheet" href="assets/dash.css">
</head>
<body>
<div id="tip" role="status" aria-live="polite"></div>
<div id="drill-backdrop"></div>
<aside id="drill-panel" aria-hidden="true" role="dialog" aria-label="Underlying posts">
  <div class="drill-hd">
    <div class="drill-hd-txt">
      <p id="drill-title">Details</p>
      <p id="drill-sub" class="fnote"></p>
    </div>
    <button type="button" id="drill-close" aria-label="Close">&times;</button>
  </div>
  <div id="drill-body"></div>
</aside>
<header class="top">
  <div class="top-in">
    <div class="brand">
      <h1>Social Signal Monitor</h1>
      <div class="sub">{esc(str(win.get('start'))[:10])} → {esc(str(win.get('end'))[:10])}
        · updated {esc(generated[:16].replace('T', ' '))} UTC</div>
    </div>
    <div class="mode-toggle" role="tablist" aria-label="Dashboard mode">
      <button type="button" id="mode-basic-btn" aria-pressed="true">Overview</button>
      <button type="button" id="mode-advanced-btn" aria-pressed="false">Detailed</button>
      <button type="button" id="mode-military-btn" aria-pressed="false">Military</button>
    </div>
    <div class="top-right">
      <span class="badge" id="mode-badge">{'real data' if mode.startswith('real') else 'fixture'}</span>
      <span class="badge gray" id="source-badge">source: {esc(source_label)}</span>
      <span class="badge gray" id="data-status">loading…</span>
      <button class="tgl" id="theme-toggle" type="button">Dark</button>
    </div>
  </div>
</header>
<div id="basic-view">{basic_view_html}</div>
<div class="wrap" id="advanced-view" style="display:none">
  <div class="filters">
    <div class="filters-in" id="filters-in"></div>
  </div>
  {fixture_banner}
  <nav class="toc" aria-label="Sections">{toc}</nav>
  {doc.html()}
  <pre id="selftest"></pre>
  <footer class="foot">
    <p>Interactive UI over a <strong>metrics snapshot</strong>
    (<code>data/metrics.json</code>) — not a live database connection.
    Filters re-scope every chart in the browser. Refresh the snapshot with
    <code>python UI/build_dashboard.py</code> after re-running metrics v2.</p>
    <p>Serve this folder over HTTP so the JSON can load:
    <code>cd UI &amp;&amp; python3 -m http.server 8877</code></p>
  </footer>
</div>
{data_boot}
<script src="assets/dash.js"></script>
<script src="assets/live.js"></script>
</body>
</html>
"""

    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Social Signal Monitor</title>
<style>
{css}
</style>
</head>
<body>
<div id="tip" role="status" aria-live="polite"></div>
<div id="drill-backdrop"></div>
<aside id="drill-panel" aria-hidden="true" role="dialog" aria-label="Underlying posts">
  <div class="drill-hd">
    <div class="drill-hd-txt">
      <p id="drill-title">Details</p>
      <p id="drill-sub" class="fnote"></p>
    </div>
    <button type="button" id="drill-close" aria-label="Close">&times;</button>
  </div>
  <div id="drill-body"></div>
</aside>
<header class="top">
  <div class="top-in">
    <div class="brand">
      <h1>Social Signal Monitor</h1>
      <div class="sub">{esc(str(win.get('start'))[:10])} → {esc(str(win.get('end'))[:10])}
        · updated {esc(generated[:16].replace('T', ' '))} UTC</div>
    </div>
    <div class="mode-toggle" role="tablist" aria-label="Dashboard mode">
      <button type="button" id="mode-basic-btn" aria-pressed="true">Overview</button>
      <button type="button" id="mode-advanced-btn" aria-pressed="false">Detailed</button>
      <button type="button" id="mode-military-btn" aria-pressed="false">Military</button>
    </div>
    <div class="top-right">
      {mode_badge}{partial}
      <span class="badge gray" id="source-badge">source: {esc(source_label)}</span>
      <span class="badge gray" id="data-status">embedded</span>
      <button class="tgl" id="theme-toggle" type="button">Dark</button>
    </div>
  </div>
</header>
<div id="basic-view">{basic_view_html}</div>
<div class="wrap" id="advanced-view" style="display:none">
  <div class="filters">
    <div class="filters-in" id="filters-in"></div>
  </div>
  {fixture_banner}
  <nav class="toc" aria-label="Sections">{toc}</nav>
  {doc.html()}
  <pre id="selftest"></pre>
  <footer class="foot">
    <p>Self-contained offline build (<code>--static-embed</code>). Filters still
    re-scope charts client-side. Data is a snapshot, not live from the DB.</p>
  </footer>
</div>
{data_boot}
{script_block}
</body>
</html>
"""


# ==========================================================================
# payload builder (snapshot JSON — not a live DB feed)
# ==========================================================================

def _scrub(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return [_scrub(v) for v in obj]
    return obj


def load_tables(db: Path, *, fixture: bool = False, log=print) -> tuple[dict, list[str], str, str]:
    """Return (tables, found, mode, source_label)."""
    tables: dict = {}
    found: list[str] = []
    mode, source_label = "fixture", "UI/fixture.py"

    if fixture:
        mode, source_label = "fixture", "UI/fixture.py (forced)"
        log("mode: FIXTURE (forced)")
    elif db.exists():
        log(f"reading {db} (read-only)")
        try:
            tables, found = load_from_duckdb(db, log)
        except Exception as exc:
            log(f"  !! could not read the database: {type(exc).__name__}: {exc}")
            tables, found = {}, []
        missing_required = [t for t in REQUIRED_FOR_BUILD if t not in tables
                            or len(tables[t]) == 0]
        if missing_required:
            log(f"  !! required tables absent/empty: {', '.join(missing_required)}")
            tables, found = {}, []
            mode, source_label = "fixture", "UI/fixture.py (metrics.duckdb incomplete)"
        else:
            complete = all(t in tables and len(tables[t]) for t in CONTRACT)
            mode = "real" if complete else "real (partial)"
            source_label = f"data/{db.name}"
    else:
        mode, source_label = "fixture", "UI/fixture.py (metrics.duckdb absent)"
        log(f"{db} does not exist yet")

    if not tables:
        log("building the seeded contract fixture (UI/fixture.py)")
        from fixture import build_fixture
        tables = build_fixture()
        found = sorted(tables)
    return tables, found, mode, source_label


def build_payload(db: Path | None = None, *, fixture: bool = False, log=print,
                  geo_db: Path | None = None) -> tuple[dict, dict]:
    """Build the metrics JSON snapshot from metrics.duckdb (or fixture).

    Returns (payload, tables). Payload is the browser data artifact; not live.
    """
    db = Path(db) if db is not None else DEFAULT_DB
    tables, found, mode, source_label = load_tables(db, fixture=fixture, log=log)
    report = check_contract(tables, log)

    geo_db = Path(geo_db) if geo_db is not None else DEFAULT_GEO_DB
    geo_tables, geo_kpi, geo_mode = load_geo_tables(geo_db, log)
    tables.update(geo_tables)  # additive; distinct table names, no collisions

    win = {"start": None, "end": None}
    rm = tables.get("run_meta")
    if rm is not None and len(rm):
        r0 = rm.iloc[0]
        win = {"start": _jsonable(r0.get("window_start")),
               "end": _jsonable(r0.get("window_end"))}
    if not win["start"] and "metrics_daily" in tables:
        md = tables["metrics_daily"]
        if "in_window" in md.columns and md["in_window"].any():
            inw = md[md["in_window"] == True]  # noqa: E712
            win = {"start": _jsonable(inw["day"].min()),
                   "end": _jsonable(inw["day"].max())}
    for k in ("start", "end"):
        if isinstance(win[k], str):
            win[k] = win[k][:10]

    payload = {
        "$schema_version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "source": source_label,
        "contract_ref": SPEC + " §5",
        "notes": JSON_NOTES,
        "window": win,
        "contract_check": report,
        "tables_present": found,
        "tables": {n: frame_to_columnar(df, n) for n, df in tables.items()},
        "snapshot": True,  # UI is dynamic; data is a frozen rebuild artifact
        "geo_mode": geo_mode,
        "geo_source": f"data/{geo_db.name}" if geo_mode != "absent" else None,
        "geo_spec_ref": GEO_SPEC,
        "geo_kpi": geo_kpi,
        "taxonomy": load_taxonomy(log),
    }
    payload["derived"] = build_derived(tables)
    return _scrub(payload), tables


# ==========================================================================
# main
# ==========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help="path to metrics.duckdb (default: data/metrics.duckdb)")
    ap.add_argument("--geo-db", default=str(DEFAULT_GEO_DB),
                    help="path to geo_ontology.duckdb (default: data/geo_ontology.duckdb)")
    ap.add_argument("--fixture", action="store_true",
                    help="ignore the database and build from UI/fixture.py")
    ap.add_argument("--out", default=str(HERE / "index.html"))
    ap.add_argument("--json-out", default=str(HERE / "data" / "metrics.json"))
    ap.add_argument("--static-embed", action="store_true",
                    help="embed full metrics JSON in index.html (offline file://). "
                         "Default is a slim shell that loads data/metrics.json.")
    args = ap.parse_args()

    def log(msg=""):
        print(msg, flush=True)

    log("=" * 72)
    payload, tables = build_payload(Path(args.db), fixture=args.fixture, log=log,
                                    geo_db=Path(args.geo_db))
    mode = payload["mode"]
    source_label = payload["source"]
    win = payload["window"]
    report = payload["contract_check"]

    out_json = Path(args.json_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=None, allow_nan=False,
                                   separators=(",", ":")), encoding="utf-8")

    # Shell uses empty fact tables so HTML is not a 4MB hardcode; dash.js
    # fetches metrics.json. Dim tables stay so methodology copy can mention them.
    if args.static_embed:
        payload["_embed_data"] = True
        html = build_html(payload, tables, mode, source_label, report)
    else:
        shell = dict(payload)
        shell["_embed_data"] = False
        shell["tables"] = {
            k: payload["tables"][k]
            for k in ("topic_dim", "platform_dim", "metric_meta", "tag_dim", "run_meta")
            if k in payload["tables"]
        }
        html = build_html(shell, tables, mode, source_label, report)

    out_html = Path(args.out)
    out_html.write_text(html, encoding="utf-8")

    log("-" * 72)
    log(f"mode            : {mode}")
    log(f"source          : {source_label}")
    log(f"window          : {win['start']} -> {win['end']}")
    k = payload["derived"]["kpi"]
    log(f"cells           : {k.get('cells_total', 0):,} "
        f"({k.get('cells_in_window', 0):,} in window, "
        f"{k.get('cells_support_ok', 0)} pass the support gate)")
    log(f"alerts          : {k.get('alert_candidates', 0)} candidates "
        f"{k.get('alerts_by_severity', {})}")
    gk = payload.get("geo_kpi") or {}
    log(f"geo-ontology    : mode={payload.get('geo_mode')} "
        f"entities={gk.get('entities_total', 0):,} "
        f"(kept {gk.get('entities_kept', 0):,}) "
        f"geo-alerts={gk.get('alerts_total', 0)} {gk.get('alerts_by_dim', {})}")
    log(f"wrote           : {out_html}  ({out_html.stat().st_size / 1024:.0f} KB)")
    log(f"wrote           : {out_json}  ({out_json.stat().st_size / 1024:.0f} KB)")
    log("UI mode         : " + (
        "static-embed (file:// ok)" if args.static_embed
        else "dynamic shell → fetch data/metrics.json (serve UI/ over HTTP)"
    ))
    log("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
