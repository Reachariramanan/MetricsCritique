# Current architecture

Snapshot of how the system is built today. For the running narrative of what changed and
why, see `collaboration/04_CURRENT_STATE.md`; for formulas, see `docs/METRICS_V2_SPEC.md`
and `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`.

---

## 1. System overview

```
┌──────────────────────────────────────────────┐
│ Postgres  shaktidb_social   (LIVE, ingesting) │
│ READ-ONLY · REPEATABLE READ · SELECT · ROLLBACK│
└───────────────────────┬──────────────────────┘
                        │ scripts/01_extract_base.py  (Layer 0)
                        ▼
┌──────────────────────────────────────────────┐
│ FOUNDATION STORE                              │
│ data/parquet/*   +   data/analytics.duckdb    │
│ post_facts · comment_facts · post_embeddings  │
│ engagement_snapshots (append-only)            │
│ collection_effort · extract_meta              │
└──────┬───────────────┬───────────────┬───────┘
       │ L1            │ L2            │ L3
       ▼               ▼               ▼
  02 diagnostics   03 ontology     04 NLP scores
  age_norm_params  topic_dim(v1)   post_scores
       └───────────────┼───────────────┘
                       │
         ┌─────────────┴──────────────────────────┐
         ▼                                        ▼
┌─────────────────────────┐        ┌──────────────────────────────────┐
│ METRICS v2              │        │ GEO-ONTOLOGY LAYER               │
│ 07_metrics_v2.py        │        │ 15 topics v4 (GPU, hierarchical) │
│   → data/metrics_stage/ │        │ 10 entities  → 13 subtypes       │
│ 08_metrics_db.py        │        │ 12 risk/theme (bi-enc + NLI)     │
│   → data/metrics.duckdb │        │ 14 CAMEO event coding            │
│                         │        │ 11 timeline metrics + alerts     │
│ (authoritative signals) │        │   → data/geo_ontology.duckdb     │
└───────────┬─────────────┘        └───────────────┬──────────────────┘
            │                                      │
            └──────────────┬───────────────────────┘
                           ▼
          ┌──────────────────────────────────┐
          │ UI/build_dashboard.py            │
          │  → UI/data/metrics.json          │
          │  → UI/data/geo_alerts.json       │
          │  → UI/index.html (shell)         │
          └──────┬────────────────────┬──────┘
                 ▼                    ▼
   UI/api_server.py (static +    scripts/notify_poller.py
   read-only /api/drill)          (reads geo_alerts.json, log-only)
                 │
                 ▼
            Browser (dash.js)

   delivery/ingest.py  ── copies metrics.duckdb + geo_ontology.duckdb ──▶
                          Docker Postgres (eda_delivery, 2 schemas + manifest)
```

Both DuckDB stores share one statistical substrate: `scripts/lib/stats_v2.py`.

---

## 2. Stores

| Store | Written by | Contents | Authority |
|-------|-----------|----------|-----------|
| `shaktidb_social` (Postgres) | upstream scrapers | Raw scraped posts, 8 platforms | Source of truth; **never written** |
| `data/parquet/*` | `01` (+ `02`–`04`) | Parquet mirrors of foundation tables; embeddings as `list<float>` | Foundation |
| `data/analytics.duckdb` | `01`–`06` | Foundation tables + v1 legacy `metrics_daily` | Foundation; v1 tables forensics-only |
| `data/metrics_stage/*.parquet` | `07` | Staged v2 tables | Intermediate |
| `data/metrics.duckdb` | `08` | Metrics v2 | **Authoritative** |
| `data/geo_ontology.duckdb` | `09`–`15` | Topics v2/v3, entities, risk/theme, CAMEO, geo timeline metrics, geo alerts | Mixed — see verdicts |
| `data/topic_ext_v4.duckdb` | `16` | `post_topic_v4_ext` (v4 + nearest-centroid fallback, top-2 topics, margin), `topic_coverage_v4` | Experimental; precision ~39%, not adopted |
| `data/gemma_ner.duckdb` | `17`, `18` | `post_route_v1` (domain routing), `entity_mentions_v2`, `post_entities_status_v2`, `entity_dim_v2` | Experimental; accuracy not yet judged |
| `data/incoming_posts.duckdb` | `watch_new_posts.py` | `incoming_posts` queue, `watch_state` | Work queue (newest posts first) |
| `data/shadow_metrics/` | `07`/`08` shadow build | Staged copy used to swap `metrics.duckdb` safely | Transient |
| `UI/data/metrics.json` | `UI/build_dashboard.py` | Browser snapshot of metrics + taxonomy | Derived |
| `UI/data/geo_alerts.json` | `11` / build | FDR-gated geo alert export | Derived |
| Delivery Postgres | `delivery/ingest.py` | Copy of both DuckDB stores + `_delivery_manifest` | Derived copy |

DuckDB is single-writer: do not run two writing stages against the same file concurrently.

---

## 3. Pipeline stages

### Foundation

| Stage | Script | Notes |
|-------|--------|-------|
| Descriptive EDA | `run_eda.py` | One REPEATABLE READ snapshot → HTML report |
| L0 extract | `01_extract_base.py` | Dedup on `(platform, native_id)`, fallback `(platform, md5(text), author)`; appends `engagement_snapshots`; rollback on per-platform failure |
| L1 diagnostics | `02_eda_diagnostics.py` | Per-platform `log1p(E) = a + b·log1p(age)` fits |
| L2 ontology v1 | `03_ontology.py` | UMAP → HDBSCAN on bge-m3; **superseded** by 09/15 (cluster count scaled with N) |
| L3 NLP | `04_nlp_scores.py` | Multilingual tone, hate (uncalibrated), readability, language |

### Metrics v2

| Stage | Script | Notes |
|-------|--------|-------|
| Build | `07_metrics_v2.py` | Grain `topic_id × platform × day`; falls back to `scraped_at` when `posted_at` is null (`day_is_scrape_proxy`) |
| Publish | `08_metrics_db.py` | Writes `metrics.duckdb` + `parquet_metrics/`, spec results report |
| Review | `review/stat_review.py` | Independent recomputation |

Tables: `metrics_daily`, `metrics_topic_day`, `tag_metrics_daily`, `burst_episodes`,
`persistence`, `alert_candidates`, `metric_meta`, `platform_dim`, `topic_dim`, `tag_dim`,
`null_control`, `sensitivity_checks`, `run_meta`.

### Experimental layers (all opt-in; defaults reproduce the legacy output)

| Stage | Script | Notes |
|-------|--------|-------|
| Topic extension | `16_assign_new_posts_topics.py` | Nearest-centroid assignment to v4 centroids for new/noise posts, top-2 topic + margin; excludes junk/incoherent targets. Dry run by default |
| Soft membership | `lib/soft_topics.py` | `07 --count-mode soft`: cross-parent ambiguous posts split by margin; constants tuned on the first half of the window only |
| HMM bursts | `lib/hmm_v1.py` | `07 --burst-method hmm`: pooled per-platform HMM, effort offset, lift capped at 20x, smoothed posteriors (causal mode for live alerting not wired) |
| Event chains | `lib/event_chains.py` | `07 --chain-method v1`: links episodes (lag, shared posts, authors); writes `event_chain_v1`, `chain_links_v1` |
| Gate harness | `review/noise_gates.py` | Burst null, stability, restatement, alert churn, precision (Wilson), verdict |
| Gemma routing | `17_gemma_route.py` | Domain D01-D09/none + runner-up, via the self-hosted Gemma (OpenAI-compatible) |
| Gemma entities | `18_gemma_entities.py` | L1 (core) + L2 (domain) entities from `data/entity_schema.json`, evidence quotes verified |
| Watcher | `watch_new_posts.py` | Read-only poll of production for newer posts; queue consumed by 17/18 before the backlog |
| Runner | `run_gemma_ner.sh` | Rounds of 17 then 18, newest first, resumable; `logs/gemma_ner.stop` stops it |

Verdicts and evidence: `docs/TOPIC_KEYING_EVALUATION.md`. `07` now orders its source reads, so
reruns are deterministic.

### Geo-ontology

| Stage | Script | Output tables |
|-------|--------|---------------|
| Topics v2 | `09_ontology_v2_cluster_fix.py` | `topic_dim_v2`, `post_topic_v2` (fixed absolute `min_cluster_size`) |
| Topics v4 | `15_topic_clustering_v4.py` | Rebuilds `topic_dim_v3` / `post_topic_v3` with parent + sub-topics (GPU) |
| Entities | `10_entity_extraction.py` | `entity_dim`, `entity_mentions` (wikineural multilingual NER + `data/gazetteer.json`) |
| Entity subtypes | `13_entity_subtype_resolution.py` | Zero-shot subtype fallback; fixes PERSON/ORG mistyping of known leaders |
| Risk / theme | `12_risk_theme_classification.py` | `risk_labels`, `theme_labels` (bi-encoder recall net → mDeBERTa NLI, threshold 0.22) |
| CAMEO | `14_cameo_event_coding.py` | Two-axis codes (theme / action), taxonomy in `data/taxonomy_cameo.json`, thresholds in `data/thresholds_cameo.json` |
| Timeline | `11_geo_timeline_metrics.py` | `entity_metrics_daily`, `risk_metrics_daily`, `theme_metrics_daily`, `geo_alert_candidates` |

---

## 4. Shared library — `scripts/lib/`

| Module | Role |
|--------|------|
| `config.py` | Paths; loads `.env`; `PG` dict (fails loudly if any `SHAKTI_PG_*` is unset); platform flags; embedding model id (`BAAI/bge-m3`, 1024-d) |
| `pg.py` | `read_only_snapshot()` context manager; `q` / `q1` helpers |
| `duck.py` | DuckDB connect, parquet write/append, table replace, views |
| `stats_v2.py` | Negative-binomial baselines, BH-FDR, empirical Bayes, AR(1), Kleinberg/CUSUM bursts, causal Savitzky–Golay, internal self-checks |
| `taxonomy.py` | Single source of truth for every displayed label (risk/theme + CAMEO); shipped inside `metrics.json` |
| `topic_version.py` | Single source of the topic table version (no env dependency; dashboard server imports it) |
| `gemma_client.py` | Stdlib OpenAI-compatible client (JSON-parse, retry with changed prompt, concurrency) |
| `entity_routing.py` | Routing/entity prompts, validators, post selection and the incoming-queue priority |
| `taxonomy_cameo.py` | Machine-readable CAMEO framework (Section A themes, Section B actions) |

---

## 5. Statistical model (summary)

| Family | Approach |
|--------|----------|
| Intensity | NB exceedance surprise against trailing baseline; BH-FDR `q`; support gates (no surprise on V ≤ 2) |
| Growth / acceleration | Effort-normalised log-rate; causal SavGol for publishable acceleration |
| Persistence / half-life | AR(1) φ on series, Ljung–Box; episode-scoped decay |
| Tone | Empirical-Bayes shrinkage toward platform mean |
| Hate | Scored but **uncalibrated** — not published |
| Virality | Age-band percentile within platform |
| Emergence | Rank combination of surprise, acceleration, novelty, speed |
| Reach | Proxy only |
| Alerts | `alert_candidates` / `geo_alert_candidates` at q < 0.05; FDR family scope set by `PARAMS["fdr_scope"]` |

Trend metrics are restricted to the collection window (`in_window`) so historical backfill
does not pose as trend.

---

## 6. Dashboard

| Piece | Behaviour |
|-------|-----------|
| `UI/index.html` | Slim shell generated at build time (layout, methodology prose from `metric_meta`) |
| `UI/assets/dash.js` | Loads `metrics.json` once; filters re-render all panels client-side; platforms derived from `platform_dim`, labels resolved through the shipped taxonomy |
| `UI/assets/dash.css` | Theme |
| `UI/api_server.py` | `ThreadingHTTPServer` on `0.0.0.0:<port>`: serves static files + `GET /api/drill`, one bounded read-only DuckDB query per click across `analytics`, `geo_ontology`, `metrics` |
| `UI/gemma_live.py` | `GET /api/gemma/status`: read-only live view of routing/entity progress (3 s cache) feeding the "Live NER" panel (5 s polling while the tab is visible) |
| `UI/fixture.py` | Seeded, schema-exact fake data if `metrics.duckdb` is missing |

Data is a **snapshot**: no polling; numbers change only after `build_dashboard.py` re-runs.
Click-to-inspect is live but read-only.

---

## 7. Alerting and notification

- Primary key: `topic_id × platform × day` (metrics v2) and `dimension_type × key × day` (geo).
- Hashtags are secondary (≈25% post coverage).
- `scripts/notify_poller.py` is a **reference** consumer of `UI/data/geo_alerts.json`; it
  de-duplicates via `reports/notify_poller_state.json` and only logs — no Slack/email/webhook
  channel has been specified.

---

## 8. Safety invariants

1. Nothing ever writes to `shaktidb_social`.
2. Credentials only from `.env` / environment; none in code or committed docs.
3. Hate prevalence is not published until calibrated.
4. Reach is always labelled a proxy.
5. v1 metrics (`05`, `06`) are never used for decisions.
6. Corpus-derived data (DuckDB, parquet, snapshots, post-text samples) is not committed.

---

## 9. Known open items

- Independent precision check of the NLI risk/theme labels.
- Entity subtype coverage after `13` should be re-measured against an outside sample.
- 6 of 33 topic clusters are flagged `coherent=false`.
- Topics are weak keys: v4 was fit on ~21k of 251k embedded posts; a full CPU recluster is not
  seed-stable (ARI 0.09-0.26); the nearest-centroid extension is ~39% precise. Topic-keyed
  metrics and alerts are provisional (`docs/TOPIC_KEYING_EVALUATION.md`).
- Gemma routing/entity accuracy is unjudged; the shared Gemma server throughput (~50 posts/min
  at concurrency 16) makes a full-backlog run take days.
- `docs/metrics-critique.md` / `scripts/audit_restatement.py`: several timeline metrics
  correlate strongly with raw volume — see that document before adding new metrics.
- Pinterest's day axis is a `scraped_at` proxy, not publish date.

## Live window and Military mode (UI)
- `UI/live_view.py` serves `GET /api/live/overview?days=7&domains=D05,D01,...`: a rolling window ending today
  (anchored on the newest data day when the corpus lags; `stale_days` reports the gap), Gemma domain routing per day
  (`gemma_ner.duckdb post_route_v1`, domain names from `data/entity_schema.json`, so new D-codes appear without code),
  and a military-related subset (primary routed domain in the chosen set; default D05, D01, D04, D06, D09; runner-up
  matches are reported as `also_touches`, not counted). Provisional: routing precision not yet judged.
- `UI/assets/live.js` adds the third mode button (Overview | Detailed | Military), the Military view and an Overview
  "Topics now" card. Days with no routed posts are hatched, not drawn as zero (the backlog is worked newest-first).
- `dash.js` now defaults the Detailed filters to the last 7 days instead of the full window.
- Harness: `UI/tests/check_live_view.mjs`.
