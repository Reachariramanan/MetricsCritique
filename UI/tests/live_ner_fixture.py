"""Fixture builder for the Live NER endpoint: temp duckdb files with the pipeline schemas."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb


def build(d: Path) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    iso = lambda m: (now - timedelta(minutes=m)).isoformat()
    g = d / "gemma_ner.duckdb"; i = d / "incoming_posts.duckdb"
    a = d / "analytics.duckdb"; s = d / "entity_schema.json"
    for p in (g, i, a):
        p.unlink(missing_ok=True)
    c = duckdb.connect(str(g))
    c.execute("""CREATE TABLE post_route_v1(post_uid VARCHAR, platform VARCHAR, lang VARCHAR, domain_1 VARCHAR,
      bucket_1 VARCHAR, confidence_1 DOUBLE, domain_2 VARCHAR, bucket_2 VARCHAR, event_mode VARCHAR, evidence VARCHAR,
      evidence_ok BOOLEAN, ok BOOLEAN, error VARCHAR, tokens INTEGER, latency_s DOUBLE, model VARCHAR, routed_at VARCHAR)""")
    c.execute("""CREATE TABLE entity_mentions_v2(mention_id VARCHAR, post_uid VARCHAR, level VARCHAR, domain VARCHAR,
      entity_type VARCHAR, name VARCHAR, canonical_name VARCHAR, entity_uid VARCHAR, uid_source VARCHAR, role VARCHAR,
      confidence DOUBLE, evidence VARCHAR, evidence_ok BOOLEAN, model VARCHAR, extracted_at VARCHAR)""")
    c.execute("""CREATE TABLE post_entities_status_v2(post_uid VARCHAR, domains VARCHAR, ok BOOLEAN, error VARCHAR,
      n_entities INTEGER, tokens INTEGER, latency_s DOUBLE, model VARCHAR, extracted_at VARCHAR)""")
    # n1 new (routed 1m ago), b1 backlog (2m), b2 failed (3m), old (60m)
    rows = [("n1", "x", "en", "D01", .9, None, iso(1), True), ("b1", "x", "en", "D02", .8, "D01", iso(2), True),
            ("b2", "tg", "en", None, None, None, iso(3), False), ("old", "tg", "en", "D01", .7, None, iso(60), True)]
    for u, pl, lg, d1, cf, d2, ra, ok in rows:
        c.execute("INSERT INTO post_route_v1 VALUES (?,?,?,?,NULL,?,?,NULL,'none',NULL,TRUE,?,NULL,10,0.5,'m',?)",
                  [u, pl, lg, d1, cf, d2, ok, ra])
    for k, (u, t, n, lv, ex) in enumerate([("n1", "ORG", "Acme", "L2", iso(1)), ("n1", "PER", "Bob", "L1", iso(1)),
                                           ("b1", "ORG", "Acme", "L2", iso(2)), ("old", "ORG", "Stale", "L2", iso(3000))]):
        c.execute("INSERT INTO entity_mentions_v2 VALUES (?,?,?,'D01',?,?,?,NULL,NULL,NULL,.9,NULL,TRUE,'m',?)",
                  [f"m{k}", u, lv, t, n, n, ex])
    for u in ("n1", "b1", "old"):
        c.execute("INSERT INTO post_entities_status_v2 VALUES (?,'D01',TRUE,NULL,1,5,.3,'m',?)", [u, iso(1)])
    c.close()
    c = duckdb.connect(str(i))
    c.execute("""CREATE TABLE incoming_posts(post_uid VARCHAR, platform VARCHAR, lang VARCHAR, text VARCHAR,
      posted_at TIMESTAMPTZ, scraped_at TIMESTAMPTZ, seen_at VARCHAR)""")
    c.execute("CREATE TABLE watch_state(platform VARCHAR, watermark TIMESTAMPTZ, last_poll VARCHAR, new_seen BIGINT)")
    long_text = "N" * 400
    for u, t in (("n1", long_text), ("n2", "pending post"), ("b2", "failed one, still not ok")):
        c.execute("INSERT INTO incoming_posts VALUES (?,?,?,?,now(),now(),?)", [u, "x", "en", t, iso(1)])
    c.execute("INSERT INTO watch_state VALUES ('x', now(), ?, 7), ('tg', now(), ?, 5)", [iso(1), iso(2)])
    c.close()
    c = duckdb.connect(str(a))
    c.execute("CREATE TABLE post_facts(post_uid VARCHAR, text VARCHAR, posted_at TIMESTAMPTZ, scraped_at TIMESTAMPTZ)")
    for u in ("b1", "old"):
        c.execute("INSERT INTO post_facts VALUES (?,?,now(),now())", [u, f"backlog text {u}"])
    c.close()
    s.write_text(json.dumps({"l2": {"D01": {"name": "Conflict Fixture"}, "D02": {"name": "Economy Fixture"}}}))
    return {"GEMMA_NER_DB": str(g), "INCOMING_DB": str(i), "ANALYTICS_DB": str(a), "ENTITY_SCHEMA_JSON": str(s)}
