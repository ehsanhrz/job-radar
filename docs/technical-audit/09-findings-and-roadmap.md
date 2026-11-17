# 09 — Findings and roadmap

Severity describes business/technical impact. Confidence describes evidence quality. Complexity is a rough engineering estimate, not a commitment.

## P0 — Critical

No confirmed P0 issue was found in the inspected source. No destructive testing or production deployment inspection was performed.

## P1 — High

### F-01 — Remote state is not geographic eligibility

- **Severity:** High · **Confidence:** High · **Category:** Product correctness
- **Evidence:** `Job.is_remote` is a nullable bool (`jobradar/models.py:30`); collectors coerce source flags to bool (`jobspy_collector.py:68`, `arbeitnow.py:39`); evaluator rewards any remote flag/text (`filters.py:86-90`). No country allowlist or eligibility enum exists in `db.py:11-34`.
- **Impact:** US-only, EU-only, and globally available remote jobs can be treated alike; an excellent technical keyword score can enter Telegram despite unknown hiring-country/payroll compatibility.
- **Likelihood:** High for international roles.
- **Recommendation:** Add evidence-backed arrangement/eligibility fields and separate manual review from high-priority delivery; do not infer Iran policy absent evidence.
- **Complexity:** Medium. **Dependencies:** agree on enum semantics and evidence retention.
- **Validation:** tests for worldwide, region/country restriction, hybrid/onsite, probation, sponsorship, contractor, explicit Iran restriction, and unknown behavior.

### F-02 — Geographic hard filters remain separate from technical review

- **Severity:** High · **Confidence:** High · **Category:** Matching quality
- **Evidence:** production ingestion uses `MatchingEngine`, which evaluates descriptions and no longer uses title inclusion as the V2 admission gate. Existing geographic/work-authorization policy remains in the legacy filter policy and produces a technical-independent exclusion.
- **Impact:** nonstandard technical titles can now reach V2, but a geographic exclusion is still an immediate delivery decision and lacks a dedicated evidence/review model.
- **Likelihood:** Medium. **Recommendation:** model geographic eligibility separately with source evidence and an unknown/review state.
- **Complexity:** Medium. **Validation:** explicit region/work-authorization/unknown cases kept separate from technical score tests.

### F-04 — Geographic fit and application priority remain outside V2

- **Severity:** High · **Confidence:** High · **Category:** Ranking
- **Evidence:** `TechnicalEvaluation` is bounded 0–100 and persists score factors, but it deliberately excludes geographic feasibility and application priority.
- **Impact:** a high score means technical fit only. It must not be read as interview probability or evidence that a remote role can hire the candidate.
- **Likelihood:** High for international vacancies. **Recommendation:** add evidence-backed geographic and priority stages in a separate milestone.
- **Complexity:** Medium. **Validation:** retain score-bound tests and add eligibility-precedence tests when that model exists.

## P2 — Medium

### F-03 — `ai.py` is dormant legacy capability, not V2 semantic matching

- **Severity:** Medium · **Confidence:** High · **Category:** Product expectation / matching
- **Evidence:** `jobradar/ai.py` contains an OpenAI-compatible reviewer; `pipeline.run_once` constructs V2 `MatchingEngine` and records the AI path as disabled for V2 rather than calling it.
- **Impact:** V2 has deterministic responsibility/requirement classification and explanation, but does not provide semantic LLM extraction. Re-enabling the old reviewer would require a deliberate integration and security review.
- **Likelihood:** Certain. **Recommendation:** keep documentation explicit about this boundary and retain deterministic eligibility safeguards outside model discretion.
- **Complexity:** Low for documentation; Medium/High for a safe optional model integration. **Validation:** provider-contract, schema/adversarial, and evidence-veto tests before activation.

### F-05 — Cross-process dedupe is not atomic

- **Severity:** Medium · **Confidence:** High · **Category:** Data integrity
- **Evidence:** `seen()` query precedes `INSERT OR IGNORE`; fingerprint index is non-unique. `RunCoordinator` removes normal service overlap, but separate CLI processes remain outside its lock.
- **Impact:** Same job under different URLs can be inserted twice during overlapping runs; SQLite primary key only prevents identical cleaned URLs. No such collision was established in the available snapshot.
- **Likelihood:** Medium under normal single-worker schedule, high if `/run` overlaps. **Recommendation:** define fingerprint collision policy, use atomic unique enforcement or transaction/lock, and coordinate runs across processes.
- **Complexity:** Medium. **Validation:** two-connection concurrent ingest test and collision test.

### F-06 — Source health conflates empty, partial, and successful discovery

- **Severity:** Medium · **Confidence:** High · **Category:** Reliability/observability
- **Evidence:** `CollectResult` only tracks aggregate calls/found/errors (`collectors/base.py:9-36`); watchdog alerts only when all recent runs found zero (`pipeline.py:34-51`). Local source-run evidence records LinkedIn 2 errors over 17 calls while finding 260 jobs.
- **Impact:** Partial 429 degradation can be missed by the all-zero alert. One run record per source hides which target/term failed.
- **Likelihood:** Medium. **Recommendation:** persist per-call outcomes and durations; alert on error rate and partial failure, distinguish empty success from request/parser failure.
- **Complexity:** Medium. **Validation:** partial-success/429 and malformed-empty tests.

### F-07 — Notification state is not atomic with Telegram delivery

- **Severity:** Medium · **Confidence:** High · **Category:** Reliability
- **Evidence:** Telegram send precedes `mark_sent` (`pipeline.py:72-78`).
- **Impact:** process/DB failure after remote send can cause a duplicate notification. There is no outbox/delivery attempt record.
- **Likelihood:** Low to medium; no incident data available. **Recommendation:** add delivery attempt/idempotency tracking or accept duplicates explicitly and expose retry state.
- **Complexity:** Medium. **Validation:** failure injection after Telegram acceptance and before DB commit.

### F-09 — Remote metadata is lossy and no review path exists

- **Severity:** Medium · **Confidence:** High · **Category:** Data model
- **Evidence:** only bool/location/description retained (`models.py:22-36`, `db.py:11-34`); statuses are filtered/low/queued/sent/application states. Local snapshot has 28 missing descriptions.
- **Impact:** source-level country lists and exact employment wording are not represented separately; unclear jobs are forced into accept/reject/low behavior.
- **Likelihood:** High for listings with sparse board metadata. **Recommendation:** preserve original arrangement/location text and extracted evidence; add review queue separate from notification queue.
- **Complexity:** Medium. **Validation:** collector normalization tests by source and migration/reevaluation tests.

### F-10 — Source pagination URL is trusted

- **Severity:** Medium · **Confidence:** Medium · **Category:** Security
- **Evidence:** `arbeitnow.collect` passes provider `links.next` directly to `session.get` (`arbeitnow.py:56-71`).
- **Impact:** A compromised or malformed API response could direct the worker to an unexpected host or internal network endpoint.
- **Likelihood:** Low; requires hostile provider response or interception outside normal TLS trust. **Recommendation:** require HTTPS and allowlist the expected API host on every page URL; cap redirects/pages.
- **Complexity:** Low. **Validation:** fake `links.next` host/redirect tests.

### F-11 — No versioned DB migrations or retention policy

- **Severity:** Medium · **Confidence:** High · **Category:** Operations/data lifecycle
- **Evidence:** schema is created inline and V2 uses safe additive column checks, but there is no ordered migration/version table; every source run persists indefinitely.
- **Impact:** future schema changes can be difficult to order/rollback; `source_runs` grows without bound.
- **Likelihood:** Medium as long as the service runs. **Recommendation:** add ordered additive migrations and documented retention/backup policy before expanding schema.
- **Complexity:** Medium. **Validation:** migrate a copy of the supplied DB and verify row/status preservation.

## P3 — Low

### F-08 — Stored application state has unrestricted string transitions

- **Severity:** Low · **Confidence:** High · **Category:** State integrity
- **Evidence:** `DB.set_status` accepts arbitrary strings and `handle_update` forwards callback status (`db.py:106-113`, `bot.py:58-69`). No DB CHECK or transition map.
- **Impact:** Unexpected callback data can create invalid states; metrics/UI silently omit unknown states.
- **Likelihood:** Low for ordinary user behavior. **Recommendation:** allowlist statuses and valid transitions, constrain schema on new DB versions.
- **Complexity:** Low. **Validation:** invalid status and transition tests.

### F-12 — Runtime/build dependencies are not reproducibly locked

- **Severity:** Low · **Confidence:** High · **Category:** Delivery/maintenance
- **Evidence:** `requirements.txt` contains one exact pin and three ranges; Docker base is `python:3.12-slim`; no lockfile, CI, or vulnerability scan was found.
- **Impact:** rebuilds may resolve different transitive versions and there is no automated test gate.
- **Likelihood:** Medium over time. **Recommendation:** use a generated lock with hashes, automate tests/security review, and use a controlled base-image update cadence.
- **Complexity:** Low/Medium. **Validation:** reproducible clean build and CI test run.

## Roadmap and acceptance criteria

### Phase 0 — Correct claims and retain evidence

- Document V2 deterministic evaluation, its separate geographic boundary, and `ai.py` as dormant legacy capability.
- Add structured source failure records and filter evidence before changing thresholds.
- Acceptance: each dropped/queued job has an inspectable deterministic reason; source partial failures are visible.

### Phase 1 — Separate suitability from eligibility

- Define geographic feasibility, eligibility, and application-priority contracts that complement the implemented technical-fit contract.
- Preserve `unknown` in a manual review queue; explicit ineligible jobs remain searchable but are not delivered.
- Acceptance: no unknown is silently labeled eligible, no explicit ineligible job enters the high-priority queue, and AWS/Kubernetes are role/requirement-contextual.

### Phase 2 — Persistence and lifecycle hardening

- Add ordered migration tracking, atomic duplicate policy, and safer delivery state; run coordination and persisted restart scheduling are implemented.
- Acceptance: existing records and application statuses survive migration; concurrent same-fingerprint ingest produces one canonical record; send failure windows are tested.

### Phase 3 — Semantic evaluation only if needed

- Add optional AI extraction/classification with schema validation, evidence citations, confidence, timeout/cost budget, prompt-injection defenses, and deterministic fallback.
- Acceptance: invalid outputs are rejected; every nontrivial decision points to job text/source evidence; provider errors cannot mark jobs eligible.

### Phase 4 — Operational quality

- Add CI, source/stage metrics, structured logs, alerting, DB backup/retention documentation, container health checks and least-privilege runtime.
- Acceptance: operator dashboard covers discovery through delivery; restore drill and clean image build are documented.
