"""Paths and connection settings for the analytics pipeline.

Production Postgres is READ-ONLY. Credentials come from `.env` (see `.env.example`)
or the environment — never hardcode secrets in committed scripts.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root: EDA/
EDA_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = EDA_ROOT / "data"
PARQUET_DIR = DATA_DIR / "parquet"
DUCKDB_PATH = DATA_DIR / "analytics.duckdb"
GEO_DB_PATH = DATA_DIR / "geo_ontology.duckdb"
# Gemma routing/entity tables live in their own file: long chunked writes must not need the
# exclusive lock on geo_ontology.duckdb that the dashboard server holds.
GEMMA_DB_PATH = DATA_DIR / "gemma_ner.duckdb"
ENTITY_SCHEMA_PATH = DATA_DIR / "entity_schema.json"
REPORTS_DIR = EDA_ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"

# Load .env explicitly by path so this works regardless of the caller's cwd.
# Does not override variables already set in the real environment.
load_dotenv(EDA_ROOT / ".env", override=False)


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in the environment."
        )
    return val


# Postgres (read-only). No hardcoded credential fallback — fail loudly instead.
PG = {
    "host": _require_env("SHAKTI_PG_HOST"),
    "port": int(_require_env("SHAKTI_PG_PORT")),
    "user": _require_env("SHAKTI_PG_USER"),
    "password": _require_env("SHAKTI_PG_PASSWORD"),
    "dbname": _require_env("SHAKTI_PG_DB"),
    "connect_timeout": int(os.environ.get("SHAKTI_PG_TIMEOUT", "30")),
}

# Platforms with known low data-quality/coverage (documentation only — the
# pipeline no longer filters these out; support gates in stats_v2 handle
# sparse/unreliable cells on a per-metric basis instead of a blanket exclude).
KNOWN_LOW_COVERAGE_PLATFORMS = frozenset({"snapchat", "pinterest", "whatsapp"})

# Deprecated alias kept only so any straggling import doesn't hard-crash;
# no code should filter on this anymore. Prefer KNOWN_LOW_COVERAGE_PLATFORMS.
TS_EXCLUDE_PLATFORMS = frozenset()

# Platforms with no usable engagement fields
NO_ENGAGEMENT_PLATFORMS = frozenset({"reddit"})

# Embedding model used in production (must match for clustering)
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# One topic basis for every reader lives in lib/topic_version.py (no env dependency, so the
# dashboard server can import it without database credentials). Re-exported here.
from .topic_version import (TOPIC_DIM_TABLE, TOPIC_EXT_TABLE, TOPIC_POST_TABLE,  # noqa: E402,F401
                            TOPIC_VERSION)


def gemma_config() -> dict:
    """Settings for the self-hosted Gemma (OpenAI-compatible vLLM) endpoint.

    Resolved lazily, not at import: most stages never call an LLM and must not fail
    because GEMMA_* is unset. No defaults -- a past run silently used a different
    deployment's model for weeks (see Entity_extraction notes), so URL and model must
    be stated explicitly in `.env`.
    """
    return {
        "url": _require_env("GEMMA_URL").rstrip("/"),
        "model": _require_env("GEMMA_MODEL"),
        "api_key": _require_env("GEMMA_API_KEY"),
        "timeout": int(os.environ.get("GEMMA_TIMEOUT", "240")),
        "concurrency": int(os.environ.get("GEMMA_MAX_CONCURRENCY", "32")),
    }
