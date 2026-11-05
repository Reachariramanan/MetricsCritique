# Current state (as of 2026-07-30, later — see top sections for the newest work)

## 2026-10-29 — Domain classifier for NER routing: BGE-M3 vs Laya (DONE; verdict PARTIAL)

`Experiments/DomainCLSTraining/` (write-up: `reports/COMPARISON.md`).
- **What it is:** a trained router for D00/D01..D09 on `data/parquet/DomainBucketing/labels_v0.parquet`.
  - The data is news title + description, with Gemma-relabelled, Claude-checked silver labels.
  - Split by date: train 12,541 / val 3,348 / test 4,058.
- **Winner: `runs/bgem3_v5`** (BGE-M3 + LoRA + auxiliary topic head + temperature).
  - Test macro-F1 0.699 [0.673, 0.722] vs Laya 0.638. Paired CI of the difference +0.034..+0.088.
  - In-scope recall 0.915, top-2 hit 0.912, ECE 0.019.
  - About 870 posts/s on GPU and 30/s on CPU (bf16). The Gemma router does about 2.3/s.
- **Laya:** only better on in-scope precision (+2 pts) and D04. It costs 5× the compute.
- **Verdict:** SOUND on the news distribution. **PARTIAL for social posts.**
  - It agrees with Gemma's in/out decision on 83% of the 11,995 routed posts (κ 0.64), but on the domain for only 56% of posts both call in-scope.
  - On hand-judged disagreements, each router was right about equally often. Gemma over-routes off-topic posts with topical words.
  - The classifier misses short off-topic Reddit chatter (it routes it in-scope) and ransomware leak posts (it routes them `none`).
- **Root cause found: the two routers use different taxonomies.** Gemma's routing prompt (`entity_routing.py:46`) gives domain names only. So wars go to D05 and riots or attacks to D09 under Gemma, while the labelled taxonomy puts both in D01 (about 480 posts).
- **Ready to use:** `predict.py` `load_router(run).route(texts, none_threshold=0.8)` returns `post_route_v1`-shaped columns. On val, the threshold of 0.8 gives 0.977 in-scope recall and the right L2 in top-2 0.932.
- `17`/`18` are untouched.
- **Open:**
  1. Hand-judge about 150 social posts as gold (over-sample Telegram, bn, ru).
  2. Add social negatives and ransomware positives, then retrain.
  3. Give Gemma's routing prompt the per-domain topic list.
  4. Decide on a `17b` / `post_route_v2` switch.
- **NER loop:** `run_gemma_ner.sh` was **paused** for this run (`logs/gemma_ner.stop`); it is not resumed.
- **Bug found (not fixed):** `entity_routing.py:108` renders types as `TYPE [L1]:`. Gemma copies the suffix into the type, so about 64% of `18` extractions fail validation (`invalid:type='LOC_COUNTRY [L1]'`). Failed posts are not marked done, so they will be retried after a fix.

## 2026-07-30, latest — Fixed-k=33 GPU (cuML) re-clustering + delivery DB refresh (DONE)

Risk/theme remediation below completed (script ran clean overnight, no crash this time).
Two more things landed after that:

1. **Topic clustering v3.** Tried GPU-accelerating the v2 clustering fix via cuML
   HDBSCAN first — a full parameter sweep found no viable region (only severe
   fragmentation or single-mega-cluster collapse; cuML's UMAP embedding geometry differs
   from `umap-learn`'s, so CPU-tuned parameters don't transfer). Pivoted to fixed-k
   clustering instead: `cuml.cluster.KMeans(n_clusters=33)` on the same 21,228 bge-m3
   embeddings, GPU memory capped at 3.5GB via RMM pool. Result is a real improvement:
   **largest cluster only 5.66% of corpus** (vs v2's 23-29%, vs the original bug's
   85-94%). 10 real posts per cluster were sampled and read (not just top-terms) to name
   and describe all 33 clusters; 27/33 are genuinely coherent single topics, 6 are marked
   `coherent=false` honestly (2 are data artifacts — dead LinkedIn pages, mistagged
   LinkedIn career content — the rest are loose miscellany). K=33 also resolved real
   structure v2's coarser 11 clusters hid: the old "Middle-East" mega-topic split into 6
   genuine sub-narratives, Russia-Ukraine into 4. Written to `topic_dim_v3`/
   `post_topic_v3` in `data/geo_ontology.duckdb` (v2 tables kept for comparison, not
   deleted). Full table and methodology: `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md` §7.
2. **Delivery Postgres refreshed.** `delivery/ingest.py` re-run against the fixed
   risk/theme labels and the new v3 clustering — 27 tables across 2 schemas +
   `_delivery_manifest` now live in the `delivery/docker-compose.yml` Postgres container.
   Manifest verdicts updated: risk/theme labels now "PARTIAL (fix applied, not yet
   re-verified)" instead of UNSOUND; topic_dim_v3/post_topic_v3 added as "PARTIAL
   (substantial improvement)".

**Still open**: an independent, fresh hand-judged precision check on the new
risk_labels/theme_labels (the fix's own calibration sample scored 72%/46%
precision/recall, but that's not the same as an outside check); entity-subtype coverage
fix (98.5% unresolved) was not reattempted this pass; dashboard UI does not yet surface
`topic_dim_v3` (it didn't surface v2 either — consistent, but worth closing if the
receiving team wants cluster names visible in the UI, not just in the delivery DB).

## 2026-07-30, later — Remediation pass on entities + risk/theme (DONE, see above)

*(Historical account of the remediation run below is kept as-is; it completed
successfully after the session-limit interruption it describes.)*

Two real problems from the build below (`entity subtype coverage`, `risk/theme UNSOUND`)
are being fixed. **First remediation attempt failed entirely** — all 5 stages hit the
session's usage limit (reset 10:20 UTC) before any completed; `agents_done=0/5`. **Nothing
was corrupted**: `data/geo_ontology.duckdb` is untouched (verified: same row counts/mtime
as the original build, no partial writes — DuckDB's transactional model meant the crash
left no half-written table). One useful artifact survived: `scripts/12_risk_theme_classification.py`
(22KB, appears complete) was written before the crash, and its own docstring documents
real calibration work already done — a fresh 105-post hand-judged sample found bi-encoder
cosine similarity alone tops out at ~48% precision even with per-category thresholds
(confirming thresholds alone can't fix this), so it pivots to a bi-encoder-recall-net +
mDeBERTa-v3 multilingual-NLI-final-decision ensemble, calibrated to NLI_THRESHOLD=0.22
(72%/46% lenient precision/recall, 61%/65% strict, on that sample) — **but this script has
not yet been run against the full corpus**, and `risk_labels`/`theme_labels` in
`data/geo_ontology.duckdb` are still the original UNSOUND flat-threshold values.
`scripts/13_entity_subtype_resolution.py` (the entity-subtype fix) was not started.
Resuming now that the session limit has reset. **Do not treat this section's "IN
PROGRESS" state as stale without checking `data/geo_ontology.duckdb` row counts /
`scripts/12_risk_theme_classification.py` mtime directly — this file may lag the live
state if a session ends mid-remediation again.**

Also landed this pass (independent, direct verification — not from an automated agent):
entity extraction's base NER (before subtyping) checks out well — two 25-post stratified
samples (gazetteer-resolved and unresolved) both hit **25/25 and 25/25** surface-form-
present-in-source-text, across English/Russian/Chinese/Bengali — closing the "precision
never measured" gap from the build below for the *base extraction* question. Two problems
found in the same pass, now targeted by the remediation above:
1. **Entity subtype coverage is real but tiny**: only **1,649/110,983 (1.5%)** of mentions
   have any subtype at all (98.5% `unresolved`) — the zero-shot subtyping fallback named in
   the taxonomy was designed but never implemented in the full run, only gazetteer matches
   on well-known names got typed.
2. **Entity-type/subtype consistency bug**: 42/1,649 (2.5%) gazetteer matches have an
   entity_type inconsistent with the assigned subtype — e.g. "Narendra Modi" correctly
   tagged `political_leader` but under `entity_type=ORG` or `LOCATION` instead of `PERSON`,
   because the upstream generic NER model inconsistently classifies famous names depending
   on sentence context. Concentrated almost entirely in `political_leader` (42/983, 4.3% of
   that subtype) and negligibly in `diplomat` (2/98); every other subtype (armed_group,
   border_town, business_org, economic_zone, international_org, military_leader, ministry,
   port, state_owned_enterprise) had **zero** mismatches.

## 2026-07-30 — Geo-ontology layer added (entity/risk/theme/timeline)

A second, additive analysis layer landed on top of everything below: topic
clustering v2, entity extraction (NER + gazetteer), risk/theme zero-shot
labels, and timeline metrics + alerts for all three new grains, in a new DB
(`data/geo_ontology.duckdb`, separate from `data/metrics.duckdb`) plus four
new dashboard sections (§16–19) and a reference notification poller. **Not
uniformly sound — full verdict-by-subsystem record, dashboard rendering
proof, and known gaps are in `docs/GEOPOLITICAL_ONTOLOGY_SPEC.md`; read it
before relying on any of `entity_dim`, `risk_labels`, or `theme_labels`.**
One-line summary: topic clustering PARTIAL (real fix, one residual
mega-cluster), entity extraction PARTIAL (precision never measured), risk/
theme labels **UNSOUND** (measured 29–45% precision, confirmed recall gaps —
do not treat as ground truth), timeline metrics for all three new grains
SOUND (46/46 self-checks, 0 mismatches on independent recomputation). This
file (04_CURRENT_STATE.md) is otherwise unchanged below — it still describes
the Metrics v2 / Layer-4 state as of 2026-07-29.

## 2026-07-29 refresh — all platforms now included, fresh extract

Two fixes landed this session, both now reflected below:

1. **Platform exclusion removed, not widened.** `lib.config.TS_EXCLUDE_PLATFORMS` is now
   an empty frozenset (deprecated alias); `KNOWN_LOW_COVERAGE_PLATFORMS` is a
   documentation-only flag, never used to filter. Audited `03_ontology.py`,
   `07_metrics_v2.py`, `08_metrics_db.py`, `UI/build_dashboard.py` — no other hardcoded
   platform lists found. But a second, non-obvious exclusion mechanism was found and
   fixed: `07_metrics_v2.py`'s `load_source()` dropped every row with a null
   `posted_at`, which silently zeroed out **100% of Pinterest** (0/78 rows have
   `posted_at` in production — a real, pre-existing data gap, not a bug in our code).
   Fixed by falling back to `scraped_at` for the day axis when `posted_at` is missing,
   flagged via `day_is_scrape_proxy` and surfaced in `platform_dim.notes` (never
   confused with a true publish date; support gates still apply). Same principle
   applied to `scripts/review/stat_review.py`, which had its own independent
   `TS_EXCLUDE = ("snapchat","pinterest","whatsapp")` tuple — removed.
2. **Fresh extract required two real bug fixes**, not just a re-run: (a)
   `01_extract_base.py`'s embedding fetch referenced a column (`embedding_model`) that
   production has since dropped from every platform table (schema drift) — the resulting
   error wasn't rolled back, poisoning the next platform's query and silently producing
   **zero rows** for instagram/pinterest/snapchat. Fixed by dropping the dead column
   reference and adding rollback-on-failure. (b) A single live row had
   `scrape_lag_days` fractionally negative (clock skew, ~19s) and tripped a hard
   `assert` in `07_metrics_v2.py` that crashed the whole run — softened to a WARN + null.

## Production source

| Field | Value |
|-------|-------|
| Host | `<PG_HOST>:13366` |
| DB | `shaktidb_social` |
| Credentials | `.env` at repo root → `lib.config.PG` (never hardcoded) |
| Character | Live-ingesting OSINT social corpus |
| Access mode | **READ-ONLY** |

## Corpus (Layer 0 extract) — `extract_id 20260729T134230Z_bd348e9f`

| Platform | post_facts (deduped) | Notes |
|----------|---------------------:|-------|
| telegram | 15,257 | Raw 23,987 (36.4% URL dups) |
| linkedin | 3,495 | Was 924 — real ~4x jump upstream |
| youtube_video | 2,344 | Embeddings **now complete** (100%, CPU bge-m3 backfill) |
| reddit | 1,518 | No engagement fields; raw==facts, 0% dedup this pull (was ~36%) |
| facebook | 238 | |
| instagram | 237 | |
| snapchat | 189 | **Included** (was excluded); 179/189 rows have neither `posted_at` nor `scraped_at` — dropped for lacking any timestamp at all, not by platform |
| pinterest | 78 | **Included** (was excluded); 0/78 have `posted_at` — day axis falls back to `scraped_at` (flagged, support-gated) |
| whatsapp | 0 | Genuinely 0 rows in production; pipeline confirmed not to crash on this |
| **Total posts** | **23,356** | (23,176 after the "no timestamp at all" drop, in `07_metrics_v2`'s TS scope) |
| youtube comments | 77,496 | `comment_facts` |
| embeddings | 21,228 (90.9%) | 18,884 pulled directly + 2,344 YouTube backfilled this run |
| snapshot extract_ids | **4** | Append path verified again (was ≥2) |

## Ontology (Layer 2)

UMAP → HDBSCAN, **3 clusters + `unassigned`** (2.4% noise on the embedded matrix; ~9.6%
of all posts unassigned including no-embedding rows). Down from 8 clusters / ~21% noise
in the prior build.

**This is a coarsening artifact, not a quality improvement.** `min_cluster_size =
max(25, len(posts_e)//80)` scales directly with corpus size; the corpus grew from
~16,600 to 21,228 embedded posts (LinkedIn alone nearly 4x'd), so HDBSCAN was forced
into fewer, larger clusters. One cluster (`t002`, "Middle-East") now absorbs **92%** of
all embedded posts — too coarse to serve topic-keyed alerting, which needs the
underlying narratives (Russia-Ukraine, Cyber, Civil/Politics/Disaster, etc.) separated,
not merged into one mega-topic. Flagging for a future session to decouple
`min_cluster_size` from raw corpus size (fixed cap, or a different scaling rule) —
not fixed here, since retuning + re-validating clustering is out of scope for this
platform-inclusion/refresh task. (A cuML/GPU-accelerated clustering path was added to
`03_ontology.py` after this session's run completed — purely additive, falls back to
the CPU path used here, does not change the numbers above.)

## NLP (Layer 3)

| Score | Coverage | Notes |
|-------|----------|-------|
| Tone | 89.4% (20,887/23,356) | Mean ≈ −0.23 |
| Hate / toxicity | 89.4% | **Uncalibrated** (`hate_calibrated=false`) |
| Readability | 58.4% | Length-gated; up from ~46% (corpus mix shifted toward longer-text platforms) |

Ran on **CPU** (GPU fully held by a vLLM process, 44/49 GB, throughout this session) via
`PYTHONPATH` pointing at the lab env's torch/transformers install (`.venv` deliberately
excludes torch — see `requirements.txt` comment).

## Metrics v2 (authoritative)

| Item | Value |
|------|------:|
| DB | `data/metrics.duckdb` |
| Daily cells | 941 (486 in-window, 211 support_ok) |
| Tag cells | 26,257 (`tag_metrics_daily`) |
| Alerts | 28 (12 high / 16 medium) |
| max surprise | 3.01 |
| Non-null surprise on V≤2 | **0** |
| Hashtag coverage | 25.5% of posts (up from ~18%) |
| platform_dim | **8 rows — every platform in `post_facts`, including snapchat & pinterest** |

**v1 `analytics.duckdb.metrics_daily` is superseded** for decisions (keep for forensics).

## Dashboard

| Item | Value |
|------|--------|
| Shell | `UI/index.html` (~41 KB) |
| Snapshot | `UI/data/metrics.json` (~10.9 MB, up from ~4 MB — larger tag panel) |
| Engine | `UI/assets/dash.js` (filters → re-render; `PLATFORMS` derived from `platform_dim`, never hardcoded) |
| Model | **Dynamic UI, snapshot data** (not live DB) |
| Serve | `cd UI && python3 -m http.server 8877` |
| Snapchat/Pinterest | Render as their own small-multiple panels (sparse: snapchat has 1 in-window row, pinterest 21 — both non-zero, so both appear) |

## Alerting

Primary: **topic_id × platform × day**  
Secondary: hashtag / keyword  
Hate: off until calibration  
See `docs/ALERTING_KEYING.md`.

## Known footguns

1. Do not use v1 robust-z leaderboards.  
2. Prefer `V_rate` / effort-normalised growth over raw V.  
3. SavGol window changes `accel_smooth` rankings (parameter-sensitive).  
4. Cluster labels need human review for briefings — **more urgent now**: 3 clusters is
   likely too coarse (see Ontology note above), revisit `min_cluster_size` before using
   these labels in a briefing.  
5. Pinterest's day axis is a `scraped_at` proxy, not true `posted_at` — trend metrics
   for pinterest are observation-day, not publish-day, series.  
6. UI numbers stale until `build_dashboard.py` re-runs.  
7. DuckDB single-writer; prod stays read-only.  
8. `.venv` has no torch/transformers by design — GPU-touching stages (`03_ontology.py`,
   `04_nlp_scores.py`) need `PYTHONPATH` pointed at a torch-capable env (see
   `05_HOW_TO_RUN.md` / `requirements.txt` comment) and fall back to CPU cleanly when
   the GPU is contended.

## 2026-10-27 — topic keying experiments (see docs/TOPIC_KEYING_EVALUATION.md)
Soft top-2 counts, HMM bursts and event chains are wired into `07` as opt-in switches
(default = legacy, byte-identical). None passed adoption gates: v4_ext fallback precision
~39% (floor 50%), cluster stability ARI <= 0.26, soft-membership alert churn unexplained,
HMM unproven on precision. `07` now orders its source reads (previously nondeterministic).
Verdict for the topic-keyed layer: **PARTIAL/UNSOUND as keys** -- read topic metrics as provisional.
