# Topic keying: evaluation of the soft / HMM / chain changes (2026-10-27)

Status: **evidence recorded; nothing adopted as default.** All new switches exist in
`scripts/07_metrics_v2.py` and default to the legacy behaviour (byte-identical output
proven on all 11 data tables, DuckDB threads=1). Method: `scripts/review/noise_gates.py`
(thresholds fixed in code before results). Judging below was done by a small LLM, not a
human, on 60-item stratified samples; treat precision as indicative.

## Findings

| # | Finding | Evidence |
|---|---------|----------|
| 1 | Metrics v2 keyed on v1 topics: ~92% of 270k posts were `unassigned` (3 topics + residual). | `analytics.post_topic` |
| 2 | v4 was clustered on ~21k posts (4.8% of embedded posts). | `post_topic_v4` |
| 3 | A CPU recluster of all 251k posts is feasible (~16 min/seed, <9 GB) but **not stable**: cross-seed ARI 0.09-0.26 at every min_cluster_size tried (100-1200); silhouette <= 0.12. v5 not adopted. | `15 --out-version v5`, 3-seed sweep |
| 4 | Nearest-centroid assignment to v4 (`16`, table `post_topic_v4_ext` in `data/topic_ext_v4.duckdb`) raises coverage to 65-70% but **precision is ~39%** (41/106 judged; Wilson 95% about 30-49%), below the project floor of 50%. Not adopted. | blind LLM-judge, two rounds (second after excluding junk/incoherent target topics) |
| 5 | Soft top-2 membership barely moves episodes (burst-day Jaccard 0.90) but adds 43 / removes 9 in-window alerts, 25 of them high-severity changes, **unexplained** -> gate FAIL. | `noise_gates.alert_churn` |
| 6 | HMM bursts pass the permutation null (real/shuffled 1.61 vs 1.21, bootstrap p05 +0.11) but differ strongly from legacy (episode Jaccard 0.29; 368 vs 756 episodes, median 9 vs 3 days) and the pooled burst lift sits at the 20x cap. No precision evidence. Not adopted. | `noise_gates.compare_burst_null` |
| 7 | Chains: library fixed so same-topic cosine (always 1.0) is not evidence; chain null for shared-post links is nearly always significant by construction. Unproven. | `lib/event_chains.py` |
| 8 | `07` was not deterministic (no ORDER BY on the source reads -> float32 sum order). Fixed. | byte-identity test |

## Decisions
- **Default unchanged:** `topic_source=v1`, `count_mode=hard`, `burst_method=kleinberg`, `chain_method=none`.
- Treat every topic-keyed metric as provisional; topics are weak keys (finding 3, 4).
- Adopting any switch requires: all gates PASS (INCONCLUSIVE blocks), a judged precision
  sample whose Wilson lower bound >= 0.5, and a spec entry.

## Not done (open)
Workstream D (Gemma routing/entities) has a working pilot (`17`, 100% valid JSON on 197
posts) but no accuracy evaluation and no full run (~20 h on the shared server).
Workstream C (risk/theme/CAMEO rebuild), topic labels (#18) and a causal HMM for live
alerting are not started.
