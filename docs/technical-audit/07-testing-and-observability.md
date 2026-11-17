# 07 — Testing and observability

## Tests present

The suite includes pipeline, service/applied-pagination, matching V2, reporting, and collector tests. Coverage includes mocked collector/Telegram behavior, V2 role and requirement scenarios, score bounds/determinism, service coordination, scheduling, and SQLite migration compatibility. Collector HTTP is mocked; no external network is used. Database tests use temporary SQLite files.

The suite was run during the audit against the audited checkout: **17 passed**. The host's pytest default plugins fail to initialize because an unrelated globally installed `pyreadline` imports removed `collections.Callable`; `python -m pytest -p no:capture -p no:debugging -q` succeeds. This plugin workaround is environment-specific, not an application test failure. No coverage report was generated.

## Gaps

- No tests for schema upgrades from a previous database, concurrency/race behavior, duplicate fingerprints across separate workers, job-send/mark crash windows, status transition validation, or stale-job reevaluation edge cases.
- No full `run_once` integration test; no Compose/container smoke test.
- No test that remote US/EU/worldwide distinctions are correct because implementation only tests basic remote bonus and hard exclusions.
- No tests for missing description behavior, malformed provider data, pagination loops, arbitrary `links.next`, HTTP 429 retry behavior in JobSpy, or partial-success health semantics.
- `ai.py` has focused parsing and failure-handling code but the current V2 production path intentionally does not invoke it. If the legacy reviewer is enabled again, add provider-contract, prompt-injection, and evidence-veto tests around the active integration path.
- Test module imports pandas and pytest. There is no CI workflow to enforce it automatically.

## Observability actually available

Logging uses standard Python logging; `-v` enables DEBUG (`__main__.py:12-19`). Collectors log calls/results and failures. Each source run persists aggregate calls, found, errors, last error. `/stats` and CLI `stats` show job status counts, source summaries, and top filter reasons. Watchdog alerts after configured consecutive zero-result runs.

Operators can inspect stored V2 evaluations through `explain` and `/review`, and logs include role/evaluation completion, low-confidence/unknown-role conditions, and score distribution. They still cannot directly answer per-term failures, per-stage latency, external HTTP latency, Telegram retries, sent-but-unmarked jobs, or model cost. There are no metrics, tracing, or structured logs.

## Recommended metrics (future)

Use bounded-cardinality labels (`source`, `stage`, `outcome`, not job/company/title): `jobradar_discovery_calls_total`, `jobradar_discovered_total`, `jobradar_normalization_rejected_total`, `jobradar_duplicates_total`, `jobradar_evaluations_total{outcome}`, `jobradar_filter_reasons_total{reason_code}`, `jobradar_delivery_total{outcome}`, `jobradar_source_errors_total{error_class}`, and histograms for stage/source duration. Add queue gauges by status and oldest queued age. If AI is introduced, track requests, token/cost totals, schema-validation failures, retries, and latency without logging full job descriptions or secrets.
