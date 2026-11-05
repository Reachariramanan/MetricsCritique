# UI verification harnesses

Headless (jsdom) renders of the **real** `UI/index.html` against the **real**
`UI/data/metrics.json`, plus API-level checks that run the **real** queries
against the **real** DuckDB. They are not unit tests of helper functions — they
boot the page the way a browser does, and hit the database the way the server
does, and assert on what actually comes out.

Run from the repo root (jsdom resolves from `./node_modules`):

    node UI/tests/check_taxonomy.mjs           #  9 checks
    node UI/tests/check_taxonomy_drop.mjs      #  6
    node UI/tests/check_dim_labels.mjs         # 16
    .venv/bin/python UI/tests/check_api_dimensions.py     # 9
    .venv/bin/python UI/tests/check_related_hierarchy.py  # 21

    .venv/bin/python UI/tests/check_live_ner.py           # 33 (fixture duckdb; env GEMMA_NER_DB/INCOMING_DB/ANALYTICS_DB/ENTITY_SCHEMA_JSON set by the test)
    LIVE_NER_DIR=<scratch> node UI/tests/check_live_ner.mjs  # 14 (needs a scratch build, see file header)

| harness | what it pins down |
|---|---|
| `check_live_ner.py` | `/api/gemma/status` contract (`UI/gemma_live.py`) against temp DuckDB files built by `live_ner_fixture.py`: counts, 24h top entities, 180-char snippets, new-vs-backlog flag, pending queue, 3 s cache, and missing-file returns `available=false` with a reason (never raises). |
| `check_live_ner.mjs` | Renders the Live NER section from canned payloads: strip, priority badge, API-supplied domain names, new highlighting, text-only snippets; `available=false` shows a calm not-running note; a 404 (static snapshot) hides content but leaves the dashboard intact. |
| `check_live_view.mjs` | Default 7-day window; Military mode button/view; stale-corpus banner; unrouted days hatched (not zero); text rendered as text; domain chip re-queries; 404 on a static snapshot degrades calmly. Run `node UI/tests/check_live_view.mjs` (14+2 checks). |
| `check_taxonomy.mjs` | The taxonomy ships inside `metrics.json`; every rendered label resolves through it; no raw snake_case key reaches the screen; the risk strip still shows all nine categories including the empty ones; CAMEO axes are declared and honestly marked not-yet-populated. |
| `check_taxonomy_drop.mjs` | **Regression test for the defect the taxonomy rewrite existed to kill.** Injects a category into the data that the taxonomy does not declare, and asserts it is *rendered and warned about* rather than silently dropped — which is what the old hardcoded `RISK_ORDER.filter(...)` did. |
| `check_dim_labels.mjs` | The `DIM_AXIS` registry covers every `dimension_type` the API can emit; each resolves through its **own** axis; drill params match `ALERT_ROUTES` (`code=` vs `category=`); and `renderRelated` emits labelled chips for a mixed-axis payload. |
| `check_api_dimensions.py` | Every `dimension_type` present in `geo_alert_candidates` routes and returns rows; declared-but-unbuilt axes answer **503 not-yet-populated**, not 400; an unknown type is rejected with a message naming the fix. |
| `check_related_hierarchy.py` | Self-exclusion covers the whole ancestor/descendant chain, not just the exact key; siblings survive; a lexical prefix twin (`05.2` vs `052`) is not caught; and no single axis can monopolise the related slots. |

## Why these exist

Every one of them asserts against a **silent** failure — the kind that leaves a
well-formed page with quietly wrong content:

- `check_taxonomy_drop` — a category the pipeline emitted but `dash.js` had not
  been updated for produced one fewer row: no console error, no gap in the
  chart, nothing in the logs.
- `check_dim_labels` — the three label resolvers were binary ternaries, so a
  CAMEO row fell through to `themeName()` (the *wrong* axis) or rendered a bare
  `05.2`. The accessors fail soft by design, so nothing throws.
- `check_related_hierarchy` — a parent code co-occurs with its own child in 100%
  of the child's posts, which is maximal lift *by construction*. Exact-key
  self-exclusion does not catch it, and the parent then tops the related list for
  every one of its sub-codes, in every slot. A tautology rendered as an insight.

> **Gap, stated plainly:** the plan lists `check_drill.mjs`, `check_related.mjs`
> and `check_filter2.mjs` as verification gates. They are **not in this repo** —
> they only ever existed in a scratchpad, so they cannot be re-run. The drill and
> related paths are now covered by `check_related_hierarchy.py` (query layer) and
> `check_dim_labels.mjs` (label + render layer); the filter path is **not**
> currently covered by anything.
