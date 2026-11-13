# MetricsCritique — ShaktiDB Social OSINT Metrics Pipeline

A read-only analytics pipeline that turns the live **`shaktidb_social`** Postgres corpus
(scraped Telegram, Twitter/X, TikTok, Truth Social, Mastodon, WhatsApp, LinkedIn, YouTube,
Reddit, Facebook, Facebook Profiles, Instagram, Snapchat, Pinterest, Baidu Tieba and Baidu Video
posts) into **statistically gated OSINT signal metrics**, a **geopolitical ontology**
(topics, entities, risk/theme labels, CAMEO event codes), FDR-gated **alerts**, and an
**interactive dashboard** with click-to-inspect drill-down.

> **Production Postgres is never written to.** Every connection is `readonly=True`,
> `REPEATABLE READ`, `SELECT`-only and ends in `ROLLBACK`. All outputs land in local
> DuckDB / parquet under `data/`.

- Architecture overview: [`current-arch.md`](current-arch.md)
- Guidance for AI coding agents: [`CLAUDE.md`](CLAUDE.md)
- Full handoff pack (history, decisions, state): [`collaboration/`](collaboration/README.md)

---

## Contents

1. [What it produces](#what-it-produces)
2. [Repository layout](#repository-layout)
3. [Setup](#setup)
4. [Running the pipeline](#running-the-pipeline)
5. [Dashboard](#dashboard)
6. [Delivery database](#delivery-database)
7. [Tests and verification](#tests-and-verification)
8. [Trust levels — what is sound and what isn't](#trust-levels)
9. [Documentation index](#documentation-index)
10. [What is not in this repository](#what-is-not-in-this-repository)

---

## What it produces

| Output | Grain | Where |
|--------|-------|-------|
| **Metrics v2** — growth, acceleration, intensity (NB surprise + BH-FDR), burst, novelty, persistence/half-life (AR(1)), tone (empirical-Bayes), virality, reach proxy, author diversity, readability | topic × platform × day; topic × day; hashtag × day | `data/metrics.duckdb` |
| **Alert candidates** — FDR-gated (q < 0.05), high/medium severity | topic × platform × day | `metrics.duckdb.alert_candidates` |
| **Topic clusters** — hierarchical parent/sub-topics on bge-m3 embeddings | post → topic | `data/geo_ontology.duckdb` (`topic_dim_v3`, `post_topic_v3`) |
| **Entities** — multilingual NER + gazetteer + zero-shot subtyping | mention / entity | `geo_ontology.duckdb` (`entity_dim`, `entity_mentions`) |
| **Risk / theme labels** — bi-encoder recall net + multilingual NLI decision | post → label | `geo_ontology.duckdb` (`risk_labels`, `theme_labels`) |
| **CAMEO event codes** — two-axis (theme / action) GDELT-compatible coding | post → code | `geo_ontology.duckdb` |
| **Geo timeline metrics + alerts** — same statistics as v2, applied to entity / risk / theme grains | dimension × day | `geo_ontology.duckdb` (`*_metrics_daily`, `geo_alert_candidates`) |
| **Dashboard** | — | `UI/` |

---

## Repository layout

```
.
├── scripts/                 Pipeline stages (numbered in run order)
│   ├── run_eda.py           Descriptive EDA report of the production DB
│   ├── 01_extract_base.py   Layer 0  — read-only extract → parquet + analytics.duckdb
│   ├── 02_eda_diagnostics.py Layer 1 — metric-readiness diagnostics
│   ├── 03_ontology.py       Layer 2  — UMAP + HDBSCAN topics (v1; superseded)
│   ├── 04_nlp_scores.py     Layer 3  — tone, hate, readability, language
│   ├── 05_metrics.py        Layer 4 v1 (LEGACY — forensics only)
│   ├── 06_validate.py       Layer 5 v1 (LEGACY — forensics only)
│   ├── 07_metrics_v2.py     Layer 4 v2 — authoritative metrics build
│   ├── 08_metrics_db.py     Layer 4 v2 — publish to metrics.duckdb
│   ├── 09_ontology_v2_cluster_fix.py  Topic clustering v2 (fixed min_cluster_size)
│   ├── 10_entity_extraction.py        NER + gazetteer
│   ├── 11_geo_timeline_metrics.py     Entity / risk / theme timeline metrics + alerts
│   ├── 12_risk_theme_classification.py Bi-encoder + NLI risk/theme labels
│   ├── 13_entity_subtype_resolution.py Zero-shot entity subtyping + type fix
│   ├── 14_cameo_event_coding.py       CAMEO two-axis event coding
│   ├── 15_topic_clustering_v4.py      GPU hierarchical topics (rebuilds topic_dim_v3)
│   ├── notify_poller.py     Reference alert poller (log-only; no channel wired)
│   ├── audit_restatement.py "Is this metric just volume?" audit
│   ├── eval_risk_theme_sample.py  Draws hand-judging samples
│   ├── lib/                 config, pg (read-only), duck, stats_v2, taxonomy(_cameo)
│   ├── review/stat_review.py Independent statistical review of metrics v2
│   └── tests/               Python regression tests
├── UI/
│   ├── build_dashboard.py   Builds index.html shell + data/metrics.json snapshot
│   ├── api_server.py        Static server + read-only /api/drill endpoint
│   ├── fixture.py           Seeded, schema-exact stand-in when metrics.duckdb is absent
│   ├── index.html, assets/  Dashboard shell, dash.js engine, dash.css
│   └── tests/               jsdom + API verification harnesses
├── delivery/                Docker Postgres copy of the results for a receiving team
├── docs/                    Specs, statistical review, ontology spec, metrics critique
├── collaboration/           Handoff pack: history, decisions, architecture, state
├── reports/                 Markdown reports (generated HTML/JSON/figures not committed)
├── grokplans/, todos/       Planning notes
├── data/                    Local analytics store (only taxonomy/gazetteer configs committed)
├── requirements.txt         Python deps (torch/transformers deliberately excluded)
└── package.json             jsdom for UI harnesses
```

---

## Setup

### 1. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` intentionally **does not** install `torch`, `transformers` or
`sentence-transformers`. The GPU-touching stages (`03`, `04`, `09`, `10`, `12`–`15`) expect
a CUDA-capable environment exposed via `PYTHONPATH`:

```bash
export PYTHONPATH=/path/to/torch-env/lib/python3.12/site-packages:$PYTHONPATH
```

If the GPU is held by another process (e.g. vLLM), force CPU with `CUDA_VISIBLE_DEVICES=`.
Some stages (`15`, cuML paths in `03`) require RAPIDS cuML.

### 2. Credentials

```bash
cp .env.example .env
# then fill in SHAKTI_PG_HOST / PORT / USER / PASSWORD / DB
```

`scripts/lib/config.py` loads `.env` from the repo root and **fails loudly** if any variable
is missing — there are no hardcoded fallbacks. Never commit `.env`.

### 3. Node (only for UI test harnesses)

```bash
npm install
```

---

## Running the pipeline

All commands run from the repo root with `.venv` active.

### Foundation (rerun when the source corpus changes)

```bash
python scripts/run_eda.py              # optional descriptive report → reports/
python scripts/01_extract_base.py      # Layer 0: extract → data/parquet + analytics.duckdb
python scripts/02_eda_diagnostics.py   # Layer 1: readiness diagnostics
python scripts/03_ontology.py          # Layer 2: topics (v1)
python scripts/04_nlp_scores.py        # Layer 3: tone / hate / readability
```

Re-run Layer 0 daily: `engagement_snapshots` is append-only and accrues the history needed
for per-post half-life.

### Metrics v2 (authoritative)

```bash
python scripts/07_metrics_v2.py        # → data/metrics_stage/*.parquet
python scripts/08_metrics_db.py        # → data/metrics.duckdb + reports/METRICS_V2_SPEC_RESULTS.md
python scripts/review/stat_review.py --json reports/stat_review_results.json
```

### Geo-ontology layer

```bash
python scripts/15_topic_clustering_v4.py          # hierarchical topics (GPU)
python scripts/10_entity_extraction.py            # NER + gazetteer
python scripts/13_entity_subtype_resolution.py    # zero-shot subtypes
python scripts/12_risk_theme_classification.py    # risk / theme labels
python scripts/14_cameo_event_coding.py           # CAMEO coding
python scripts/11_geo_timeline_metrics.py         # timeline metrics + geo alerts
```

### Dashboard snapshot

```bash
python UI/build_dashboard.py
```

---

## Dashboard

The dashboard is a **dynamic UI over a snapshot**: charts and filters re-render in the
browser from `UI/data/metrics.json`; numbers change only when the snapshot is rebuilt.
Clicking any mark issues one bounded, read-only DuckDB query via `api_server.py`.

```bash
python UI/build_dashboard.py                 # rebuild snapshot + shell
cd UI && ../.venv/bin/python3 api_server.py 8877
# open http://127.0.0.1:8877/
```

- A plain `python3 -m http.server` also renders the dashboard, but click-to-inspect will
  report that the live data API is unreachable.
- `python UI/build_dashboard.py --static-embed` writes a single offline HTML with data
  embedded (no click-to-inspect).
- Without `data/metrics.duckdb`, `UI/fixture.py` supplies a seeded, schema-exact stand-in
  so the UI can still be built and checked.

`api_server.py` binds to `0.0.0.0` — it is reachable from the LAN. Run it only on trusted
networks.

---

## Delivery database

`delivery/` packages the results as a Postgres container for a downstream team:

```bash
cd delivery
cp .env.example .env        # or create .env with POSTGRES_USER/PASSWORD/DB/PORT
docker compose up -d
python3 ingest.py           # idempotent full refresh from the two DuckDB files
```

Read `public._delivery_manifest` first — every table carries a SOUND / PARTIAL / UNSOUND
verdict and caveat. See [`delivery/README.md`](delivery/README.md).

---

## Tests and verification

```bash
# Python regression tests
python -m pytest scripts/tests

# UI harnesses (headless render of the real page against the real snapshot)
node UI/tests/check_taxonomy.mjs
node UI/tests/check_taxonomy_drop.mjs
node UI/tests/check_dim_labels.mjs
python UI/tests/check_api_dimensions.py
python UI/tests/check_related_hierarchy.py
```

The UI harnesses need a built `UI/data/metrics.json` and the DuckDB files. `stats_v2.py`
also runs its own internal self-checks on every metrics build.

---

## Trust levels

| Component | Status | Notes |
|-----------|--------|-------|
| Metrics v2 (`metrics.duckdb`) | **SOUND** | 46/46 statistical self-checks; independent recomputation, 0 mismatches |
| Geo timeline statistics | **SOUND math** | Output quality bounded by label quality of its inputs |
| Topic clustering v3/v4 | PARTIAL | Largest cluster ~5.7% of corpus; 6 of 33 clusters marked `coherent=false` |
| Entity extraction | PARTIAL | Base NER verified 50/50 on samples; subtype coverage historically low |
| Risk / theme labels | PARTIAL | NLI ensemble ~72% / 46% precision / recall on its own calibration sample; no outside check yet |
| Hate / toxicity | **Uncalibrated** | `hate_calibrated=false` — do not publish prevalence |
| Reach | Proxy only | Label as a proxy in any external report |
| v1 metrics (`05`, `06`) | **Superseded** | Five known defects — forensics only |

Alerts should be keyed on **`topic_id`**, not hashtags (hashtags cover only ~25% of posts).
See [`docs/ALERTING_KEYING.md`](docs/ALERTING_KEYING.md).

---

## Documentation index

| Document | Content |
|----------|---------|
| [`current-arch.md`](current-arch.md) | Current architecture: data flow, stores, components |
| [`docs/METRICS_V2_SPEC.md`](docs/METRICS_V2_SPEC.md) | Metrics v2 contract — formulas, schema, gates |
| [`docs/STATISTICAL_REVIEW.md`](docs/STATISTICAL_REVIEW.md) | Why v1 failed; how v2 closes each defect |
| [`docs/STATISTICAL_APPENDIX.md`](docs/STATISTICAL_APPENDIX.md) | Statistical details |
| [`docs/metrics-critique.md`](docs/metrics-critique.md) | Adversarial critique vs the sibling GDELT project |
| [`docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`](docs/GEOPOLITICAL_ONTOLOGY_SPEC.md) | Ontology layer spec + per-subsystem verdicts |
| [`docs/GEO_ONTOLOGY_CONTEXT.md`](docs/GEO_ONTOLOGY_CONTEXT.md) | Ontology task context |
| [`docs/ALERTING_KEYING.md`](docs/ALERTING_KEYING.md) | Alert keying rules |
| [`docs/NOTIFICATION_POLLER_AUDIT.md`](docs/NOTIFICATION_POLLER_AUDIT.md) | Notification poller audit |
| [`collaboration/`](collaboration/README.md) | Session history, decisions, how-to-run, current state |

---

## What is not in this repository

This repository holds **code and documentation only**. These are excluded via `.gitignore`
and must be regenerated locally by running the pipeline against the production corpus:

- `data/*.duckdb`, `data/parquet*/`, `data/metrics_stage/` — corpus-derived stores
- `UI/data/*.json` — dashboard snapshot
- `reports/EDA_Report.html`, `reports/figures/`, `reports/*.json|csv|log`
- `data/calibration_cameo.csv` — hand-judging sample containing raw post text
- `.env`, `delivery/.env` — credentials
- `logs/`, `.venv/`, `node_modules/`
