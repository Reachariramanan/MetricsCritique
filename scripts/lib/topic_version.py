"""Single source of truth for which topic clustering every reader serves.

Deliberately free of `.env` / Postgres imports: the dashboard server and build script
import it, and they must run on a host that has no database credentials.

Tables live in `geo_ontology.duckdb`. v1-v3 stay in the stores for comparison only
(docs/GEOPOLITICAL_ONTOLOGY_SPEC.md); a harness (scripts/tests/test_topic_version.py)
fails if a reader hardcodes a different version.
"""
TOPIC_VERSION = "v4"
TOPIC_POST_TABLE = f"post_topic_{TOPIC_VERSION}"
TOPIC_DIM_TABLE = f"topic_dim_{TOPIC_VERSION}"
# Effective assignment incl. nearest-centroid fallback + runner-up (scripts/16_*).
TOPIC_EXT_TABLE = f"{TOPIC_POST_TABLE}_ext"
# The extension table lives in its OWN duckdb file so writing it never needs the exclusive
# lock on geo_ontology.duckdb (the dashboard server holds that open). Readers ATTACH it.
TOPIC_EXT_DB_NAME = "topic_ext_v4.duckdb"
