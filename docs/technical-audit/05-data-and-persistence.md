# 05 — Data and persistence

## Schema

SQLite schema is declared inline in `jobradar/db.py:10-54` and created with `CREATE TABLE IF NOT EXISTS` in `DB.__init__` (`db.py:60-67`). There is no versioned migration framework.

| Table | Contents | Constraints/indexes |
|---|---|---|
| `jobs` | normalized job attributes, integer score, JSON reasons, status, filter reason, first-seen/sent timestamps and Telegram message id | `id` primary key; `fingerprint` index; `status` index |
| `source_runs` | timestamp, source, calls, found, errors, last error | autoincrement PK; `(source,id)` index |
| `meta` | Telegram offset and watchdog flags | key primary key |

There are no foreign keys, explicit CHECK constraints, normalized evaluation history, source/job relation, or separate application entity. `reasons` is JSON text; job sources/targets and arrangement are strings. `score` and current evaluation overwrite prior evaluation during `rescore`; there is no score history.

## Data integrity and queries

- URL ID is SHA-1(url) truncated to 16 hex characters; LinkedIn tracking query is stripped (`jobradar/models.py:13-20, 39-46`).
- Fingerprint is normalized company + title + first location segment, SHA-1 truncated to 16 characters (`models.py:48-52`). It is not unique.
- `DB.seen` performs `SELECT ... WHERE id=? OR fingerprint=?` (`db.py:70-75`). `insert` then does `INSERT OR IGNORE`, where ignore protects only the primary key, not fingerprint (`db.py:77-92`). Concurrent cross-board inserts can create duplicate fingerprints.
- SQLite uses WAL and a 30-second connection timeout (`db.py:62-66`). Each write method commits independently. The send-to-Telegram operation and status update are separate systems and cannot share a transaction.
- `queued()` uses status and score order. `by_status()` dynamically builds placeholder count and passes values as parameters, so status values are not directly interpolated (`db.py:93-121`).
- Only indexes are fingerprint, status, and source-run source/id. No index exists for first_seen, updated_at, date_posted, score, or status+score composite. Current data is small (1,158 jobs), so performance impact has not been measured.
- No retention/deletion policy is implemented; source runs and jobs grow without bound.

## Existing data snapshot

Read-only aggregate inspection found 1,158 jobs: `filtered=963`, `low=94`, `queued=81`, `sent=20`. Source/site counts: Arbeitnow 884, JobSpy LinkedIn 214, Indeed 57, Bayt 3. 28 descriptions are null; all URLs are non-null; no `is_remote` values are null. Filter reason `title: no include match` accounts for 930 filtered records. Stored score averages/maxima: low 13.89/28, queued 46.96/73, sent 92.6/118. These values confirm that scores are unbounded configured points, not percentages. Only aggregate data was read; no job text was exported.

`source_runs` has one recorded run per source in this snapshot, on 2026-11-01. LinkedIn: 17 calls, 260 results, 2 errors; last error is a 429/proxy block. Indeed: 12 calls, 57 results, 0 errors; Bayt: 6/3/0; Arbeitnow: 1 call, 900 results, 0 errors. Stored jobs are fewer than collected results due dedupe/filtering and are not evidence that all 900 results were persisted.

## ER diagram

See the ER diagram in [10 — Architecture diagrams](10-architecture-diagrams.md). `jobs`, `source_runs`, and `meta` are independent tables; there are no declared relations.

## Suitability for future separated evaluation

Current columns cannot preserve technical fit, geographic feasibility, application priority, arrangement, eligibility, confidence, or evidence independently. A safe evolution would use additive migration(s), retain legacy score/reasons as historical values or a versioned evaluation record, and ensure old queued jobs are reevaluated/held for review before new delivery. The application status values (`sent`, `applied`, `skipped`, `saved`) must remain distinct from evaluation outcomes. This is a recommendation, not implemented schema.
