# 05 — Data and persistence

## Schema

SQLite schema is declared inline in `jobradar/db.py:10-54` and created with `CREATE TABLE IF NOT EXISTS` in `DB.__init__` (`db.py:60-67`). There is no versioned migration framework.

| Table | Contents | Constraints/indexes |
|---|---|---|
| `jobs` | normalized job attributes, legacy score/reasons, status, timestamps, Telegram id, and additive V2 evaluation fields | `id` primary key; `fingerprint` index; `status` index |
| `source_runs` | timestamp, source, calls, found, errors, last error | autoincrement PK; `(source,id)` index |
| `meta` | Telegram offset, watchdog flags, and persisted service schedule values such as `last_run_at`/`last_run_finished_at` | key primary key |

V2 adds `evaluation_version`, `technical_fit_score`, `role_family`, `seniority`, `confidence`, `match_decision`, and `evaluation_details` through safe additive SQLite migration. Legacy `score` values remain historical and are not treated as V2 percentages. There are no foreign keys, explicit CHECK constraints, normalized evaluation history, source/job relation, or separate application entity.

## Data integrity and queries

- URL ID is SHA-1(url) truncated to 16 hex characters; LinkedIn tracking query is stripped (`jobradar/models.py:13-20, 39-46`).
- Fingerprint is normalized company + title + first location segment, SHA-1 truncated to 16 characters (`models.py:48-52`). It is not unique.
- `DB.seen` performs `SELECT ... WHERE id=? OR fingerprint=?` (`db.py:70-75`). `insert` then does `INSERT OR IGNORE`, where ignore protects only the primary key, not fingerprint (`db.py:77-92`). Concurrent cross-board inserts can create duplicate fingerprints.
- SQLite uses WAL and a 30-second connection timeout (`db.py:62-66`). Each write method commits independently. The send-to-Telegram operation and status update are separate systems and cannot share a transaction.
- V2 `queued()` can require an evaluation version and orders by technical fit. `/applied` uses exact status count plus paged query with the same predicate.
- Only indexes are fingerprint, status, and source-run source/id. No index exists for first_seen, updated_at, date_posted, score, or status+score composite. Current data is small (1,158 jobs), so performance impact has not been measured.
- No retention/deletion policy is implemented; source runs and jobs grow without bound.

## Existing data snapshot

Read-only aggregate inspection found 1,158 jobs: `filtered=963`, `low=94`, `queued=81`, `sent=20`. Source/site counts: Arbeitnow 884, JobSpy LinkedIn 214, Indeed 57, Bayt 3. 28 descriptions are null; all URLs are non-null; no `is_remote` values are null. Filter reason `title: no include match` accounts for 930 filtered records. Stored score averages/maxima: low 13.89/28, queued 46.96/73, sent 92.6/118. These values confirm that scores are unbounded configured points, not percentages. Only aggregate data was read; no job text was exported.

`source_runs` has one recorded run per source in this snapshot, on 2026-11-01. LinkedIn: 17 calls, 260 results, 2 errors; last error is a 429/proxy block. Indeed: 12 calls, 57 results, 0 errors; Bayt: 6/3/0; Arbeitnow: 1 call, 900 results, 0 errors. Stored jobs are fewer than collected results due dedupe/filtering and are not evidence that all 900 results were persisted.

## ER diagram

See the ER diagram in [10 — Architecture diagrams](10-architecture-diagrams.md). `jobs`, `source_runs`, and `meta` are independent tables; there are no declared relations.

## Evaluation compatibility

V2 persists technical fit, confidence, role, seniority, decision, and structured evidence independently of application status. Geography/eligibility and application priority still have no dedicated model. Application statuses (`sent`, `applied`, `skipped`, `saved`) remain distinct from evaluation outcomes.
