# 02 — End-to-end pipeline

## Current lifecycle

The operational path is `main → run_once → collect_all → source collectors → CollectResult → source health persistence → ingest/evaluate → SQLite → queued selection → Telegram → user callback → status update`.

1. **Start and settings.** `main()` dispatches a command. `run_once` opens SQLite and (unless dry-run) constructs Telegram (`jobradar/__main__.py:111-137`, `jobradar/pipeline.py:83-88`). In non-dry mode, Telegram configuration is mandatory even if the operator only wants a local scan.
2. **Drain old Telegram actions.** `process_pending` reads the stored Telegram offset, fetches pending updates, handles them, then saves each next offset (`jobradar/pipeline.py:89-92`, `jobradar/bot.py:106-124`). Update errors are logged but the offset still advances, so a failed callback can be lost rather than retried.
3. **Discover.** `collect_all` runs JobSpy first, then Arbeitnow sequentially (`jobradar/collectors/__init__.py:7-10`). JobSpy loops over target × search term × site, sleeps between calls, calls `scrape_jobs`, normalizes records (`jobradar/collectors/jobspy_collector.py:75-143`). Arbeitnow follows `links.next` up to configured page count (`jobradar/collectors/arbeitnow.py:45-75`).
4. **Aggregate source health.** Each collector returns `CollectResult`; one call records attempted calls, result count, and last error per source (`jobradar/collectors/base.py:9-36`). `record_health` inserts one source-run record per source per overall run (`jobradar/pipeline.py:29-32`).
5. **Dedupe and evaluate.** `ingest` checks `DB.seen` by URL-derived ID or company/title/city fingerprint. Existing jobs are skipped. New jobs go through `Evaluator.evaluate`, then get inserted with status `filtered`, `low`, or `queued` (`jobradar/pipeline.py:15-26`).
6. **Deliver.** `send_queued` selects up to `telegram.max_per_run`, ordered by score, renders a message and buttons, and sends one at a time. Telegram errors break the loop; the remaining rows remain queued. A successful send is followed by a status update to `sent` (`jobradar/pipeline.py:63-80`). In dry-run, messages are printed and state stays queued.
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

An individual URL is mostly idempotent because URL SHA-1 prefix is the primary key and inserts use `INSERT OR IGNORE`. Cross-board duplicate idempotency is weaker: fingerprint is indexed but not unique, and `seen()` then `insert()` are separate operations. Two processes can race. Delivery also has a send/mark gap. DB statuses have no declared CHECK constraint or transition table; `set_status` accepts any string. The bot accepts callbacks only from the configured chat, but it does not validate status values or legal transitions.

## Operations and observability

Failures are visible in logs and source-run counters, but a zero result is considered “dead” regardless of whether it is an authentic empty search or a malformed-success response. A source with some results and some errors is reported as successful enough to avoid the zero-run alert. There are no per-search-term counters, duplicate counts per source, duration per source, retry metrics, or count of jobs omitted for missing URL/title.
