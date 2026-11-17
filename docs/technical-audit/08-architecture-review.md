# 08 — Architecture review

## What is working

- Small module count and direct control flow make the application understandable.
- Source parsing has injectable seams: JobSpy accepts `scrape_fn` and `sleep_fn`; Arbeitnow accepts a session; tests use fakes.
- `Job` separates common normalized source attributes from collector-specific wire schemas.
- SQLite and Telegram are wrapped behind small classes; most business code is testable without Telegram credentials.
- Settings have a single loading point, and secrets are not placed in YAML.

## Main design constraints

### V2 matching has explicit policy stages

`matching.py` now has a typed `TechnicalEvaluation` and independently testable role classification, requirement extraction, skill matching, seniority, and scoring stages. `filters.Evaluator` remains as legacy compatibility code. Geographic policy still lacks the same explicit result contract and should not be folded into V2 technical fit.

### Raw string statuses are the state model

Statuses are comments and strings. `set_status(job_id, status)` does not validate allowed values or transitions. V2 adds a `low` review path exposed through `/review`, while application statuses remain distinct from evaluation fields. A small enum/transition map and DB constraint would still prevent typos.

### Persistence is a repository mixed with schema setup

`DB` owns connection, schema bootstrapping, serialization, queries, and commits. This is tolerable at this size. V2 has safe additive column migration and tests against a copied old DB, but schema changes still have no explicit ordered migration record.

### Pipeline uses orchestration plus side effects

`run_once` coordinates all stages and sends notifications. `ingest` writes per job; notification/outbox is not separated. A proper delivery record/outbox is only justified if duplicate/lost notifications become a material operational issue; avoid premature service decomposition.

### Production service coordinates in-process work

`service` starts one poller and uses `RunCoordinator` for scheduled and manual work; a concurrent manual request is rejected rather than queued. The `meta.last_run_at` value survives deployment restart and drives the next schedule. Separate external CLI processes still have no distributed lock or idempotent source-run key.

## Coupling and dependency direction

The call graph is acyclic in the inspected modules. `bot` imports pipeline lazily in `_threaded_run`; pipeline imports bot lazily to process pending updates. This avoids an import cycle but shows cross-layer coupling. The `Job` model contains source-normalized data and scoring output; no persistence concern leaks into it. `filters` imports `Job`, while `pipeline` depends on all stages. Graphify reflects this as one core cluster around `pipeline.py`/`bot.py`/Telegram and a cluster around `Evaluator`/Job.

## Design recommendations

1. Add `GeographicEvaluation` and `PriorityDecision` only when their product rules are agreed. Keep the existing deterministic V2 technical policy separate from optional AI extraction.
2. Make source adapters return normalized records plus explicit source call outcomes (success-empty, partial, failure), preserving original payload facts where needed.
3. Keep SQLite for the single-user workload, add explicit versioned additive migrations and unique constraints only after defining dedupe semantics.
4. Add an outbox or safe send marker protocol only if delivery duplication is observed; first add metrics and failure-injection tests.
5. Add cross-process run coordination before adding parallel collectors, a second service process, or more workers.

No recommendation here implies rewriting the application. The current deployment and data scale do not justify distributed services or a message broker based on available evidence.
