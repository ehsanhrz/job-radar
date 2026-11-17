# 02 — End-to-end pipeline

## Current lifecycle

The production operational path is `main service → service.run_service → one Telegram poller + RunCoordinator → run_once → collect_all → source collectors/ATS boards → CollectResult → source health persistence → V2 ingest/evaluate → SQLite → versioned queued selection → Telegram → user callback → status update`.

1. **Start and scheduling.** `service` initializes SQLite, starts one Telegram poller, and uses `RunCoordinator` for both scheduled and manual work. `last_run_at` is persisted in `meta` before a real run. On a fresh database service runs immediately; after a restart it waits for the remaining configured interval, including configured jitter, rather than repeating the just-started run.
2. **Telegram updates.** The polling bot owns `getUpdates`, offset handling, commands, and callbacks. Scheduled pipeline work runs with `process_updates=False`, so it does not compete for Telegram updates.
3. **Discover.** `collect_all` runs JobSpy first, then Arbeitnow sequentially (`jobradar/collectors/__init__.py:7-10`). JobSpy loops over target × search term × site, sleeps between calls, calls `scrape_jobs`, normalizes records (`jobradar/collectors/jobspy_collector.py:75-143`). Arbeitnow follows `links.next` up to configured page count (`jobradar/collectors/arbeitnow.py:45-75`).
4. **Aggregate source health.** Each collector returns `CollectResult`; one call records attempted calls, result count, and last error per source (`jobradar/collectors/base.py:9-36`). `record_health` inserts one source-run record per source per overall run (`jobradar/pipeline.py:29-32`).
5. **Dedupe and evaluate.** `ingest` checks `DB.seen` by URL-derived ID or company/title/city fingerprint. New jobs go through `MatchingEngine.evaluate`, then persist the V2 evaluation with `filtered`, `low`, or `queued` status. Pending legacy queued/low records can be upgraded without changing sent or application history.
6. **Deliver.** `send_queued` selects V2 queued jobs only, ordered by normalized technical fit, renders a compact explanation and buttons, and sends one at a time. Telegram errors break the loop; the remaining rows remain queued. A successful send is followed by a status update to `sent`. In dry-run, messages are printed and state stays queued.
7. **Watchdog and summary.** Three consecutive zero-found runs for a source trigger one alert, and a positive result resets that alert (`jobradar/pipeline.py:34-60`). Run summary contains new/sent/low/filtered/duplicate counts and source calls/found/errors (`jobradar/pipeline.py:101-120`).
8. **Application tracking.** Telegram callbacks update status to `applied`, `skipped`, `saved`, or `sent` via `DB.set_status` (`jobradar/bot.py:50-72`). These are user-entered tracking states, not a verified employer outcome.

## Failure and retry behavior

- JobSpy exceptions are logged and counted, then other searches continue. Errors exposed through DataFrame `attrs.errors` are recorded (`jobradar/collectors/jobspy_collector.py:124-141`). It has polite inter-call delay but no application-level retry loop.
- Arbeitnow catches request, status, and JSON errors, stops pagination for that source, reports one error, and retains jobs already accumulated (`jobradar/collectors/arbeitnow.py:56-75`). The next scheduled run is the only retry mechanism.
- Telegram HTTP request exceptions retry up to three times with fixed three-second sleeps. HTTP 429 retries using Telegram `retry_after`; other HTTP/API errors fail immediately (`jobradar/telegram.py:36-54`).
- There is no durable outbox. A send could succeed remotely but fail before `mark_sent`, causing a repeated send on the next run. Local status writes are SQLite transactions, but sending and marking are not atomic.
- Collector execution is synchronous and uncancellable inside a source call. JobSpy timeout behavior is delegated to its library and was not independently measured.

## Current flow diagram

See the “current execution” flowchart in [10 — Architecture diagrams](10-architecture-diagrams.md).

## Idempotency and state

An individual URL is mostly idempotent because URL SHA-1 prefix is the primary key and inserts use `INSERT OR IGNORE`. Cross-board duplicate idempotency is weaker: fingerprint is indexed but not unique, and `seen()` then `insert()` are separate operations. The unified runtime removes its former worker/bot overlap, but separate CLI processes can still race. Delivery also has a send/mark gap. DB statuses have no declared CHECK constraint or transition table; `set_status` accepts any string.

## Operations and observability

Failures are visible in logs and source-run counters, but a zero result is considered “dead” regardless of whether it is an authentic empty search or a malformed-success response. A source with some results and some errors is reported as successful enough to avoid the zero-run alert. There are no per-search-term counters, duplicate counts per source, duration per source, retry metrics, or count of jobs omitted for missing URL/title.
