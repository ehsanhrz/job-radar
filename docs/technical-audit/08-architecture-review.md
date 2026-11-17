# 08 — Architecture review

## What is working

- Small module count and direct control flow make the application understandable.
- Source parsing has injectable seams: JobSpy accepts `scrape_fn` and `sleep_fn`; Arbeitnow accepts a session; tests use fakes.
- `Job` separates common normalized source attributes from collector-specific wire schemas.
- SQLite and Telegram are wrapped behind small classes; most business code is testable without Telegram credentials.
- Settings have a single loading point, and secrets are not placed in YAML.

## Main design constraints

### Evaluator is the policy center

`filters.Evaluator` combines title gates, hard exclusion, keyword scoring, freshness, employment hints, and the queued/low decision (`filters.py:37-103`). This is not a “god class” by line count, but it owns all policy and receives an untyped raw config dictionary. A new geographic rule, AI outcome, or role-family distinction would expand it and raise the chance that a matching rule silently changes another outcome. The solution is a small explicit evaluation result and separately testable policy stages, not a broad framework.

### Raw string statuses are the state model

Statuses are comments and strings. `set_status(job_id, status)` does not validate allowed values or transitions (`db.py:106-113`). A small enum/transition map and DB constraint would prevent typos and make application tracking distinct from match evaluation. The current statuses are adequate for a prototype, but the matching workflow has no review state.

### Persistence is a repository mixed with schema setup

`DB` owns connection, schema bootstrapping, serialization, queries, and commits (`db.py:60-170`). This is tolerable at this size. Schema changes have no explicit version or migration record, which complicates future additive evaluation fields and testing from old DB files.

### Pipeline uses orchestration plus side effects

`run_once` coordinates all stages and sends notifications. `ingest` writes per job; notification/outbox is not separated. A proper delivery record/outbox is only justified if duplicate/lost notifications become a material operational issue; avoid premature service decomposition.

### Deployment has process-level coordination gaps

Compose shares SQLite between worker and bot. `/run` starts a daemon thread but its lock is process-local. There is no distributed lock, lease, or idempotent source-run key. Starting another run can duplicate external load and race cross-board dedupe.

## Coupling and dependency direction

The call graph is acyclic in the inspected modules. `bot` imports pipeline lazily in `_threaded_run`; pipeline imports bot lazily to process pending updates. This avoids an import cycle but shows cross-layer coupling. The `Job` model contains source-normalized data and scoring output; no persistence concern leaks into it. `filters` imports `Job`, while `pipeline` depends on all stages. Graphify reflects this as one core cluster around `pipeline.py`/`bot.py`/Telegram and a cluster around `Evaluator`/Job.

## Design recommendations

1. Introduce typed `TechnicalEvaluation`, `GeographicEvaluation`, and `PriorityDecision` records only when the product rules are agreed. Keep a deterministic policy engine separate from optional AI extraction.
2. Make source adapters return normalized records plus explicit source call outcomes (success-empty, partial, failure), preserving original payload facts where needed.
3. Keep SQLite for the single-user workload, add explicit versioned additive migrations and unique constraints only after defining dedupe semantics.
4. Add an outbox or safe send marker protocol only if delivery duplication is observed; first add metrics and failure-injection tests.
5. Add cross-process run coordination before adding parallel collectors or more workers.

No recommendation here implies rewriting the application. The current deployment and data scale do not justify distributed services or a message broker based on available evidence.
