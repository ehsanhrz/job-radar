# JobRadar technical audit

**Audit date:** 2026-11-17  
**Scope:** Current Python checkout, `config.yaml`, deployment files, tests, Graphify graph, and aggregate-only read-only inspection of `data/jobradar.db`.

## Executive summary

JobRadar is a small single-user job discovery application. It uses JobSpy for configured job-board searches, the Arbeitnow JSON API, SQLite for persistence, and Telegram for delivery and application-status buttons. The main flow is synchronous and easy to follow, but the product description overstates the current system: there is no AI evaluator, no explicit geographic eligibility model, and no separate technical-fit and feasibility scores. “Remote” is an unqualified boolean/keyword bonus, while the configured hard filters can reject jobs based on broad country/work-authorization terms before any deeper review.

The most consequential mismatch is that the system optimizes keyword score, not realistically accessible interviews. Current configured score weights can exceed 100 and are dominated by title matches. Existing stored data demonstrates that this is operationally material: the local database has 1,158 jobs, 963 filtered, 94 low, 81 queued, and 20 sent; among queued jobs only eight have `is_remote=1`, and no field records whether any job can hire an Iran resident. The persisted scores for sent jobs average 92.6 and reach 118, showing that score values are unbounded weighted totals, not percentages.

No code or application behavior was changed for this audit. The report directory and the Graphify graph are the audit artifacts. Earlier task-turn code edits were reverted before this investigation; the unrelated pre-existing untracked workspace files were preserved.

## Overall assessment

**Architecture:** compact, understandable, suitable for a personal prototype.  
**Business correctness for this candidate:** low to moderate. It finds relevant stack keywords but cannot determine if an employer can engage a resident in Iran.  
**Reliability:** adequate for one local run or a small VPS, with weak source observability and no cross-process dedupe guarantee.  
**Security posture:** basic secret separation exists; external content and pagination links are trusted more than a hardened production service should trust them.  
**AI:** absent in code. README roadmap explicitly lists an optional LLM pass as future work.

## Key conclusions

1. Only two collector implementations exist: JobSpy and Arbeitnow. LinkedIn, Indeed, and Bayt are configured JobSpy targets. Himalayas, Jobicy, Remotive, Jaabz, Hacker News, RemoteOK, We Work Remotely, Ashby, Lever, and Workable have no active collector in the application.
2. `Evaluator.evaluate` applies title include/exclude and hard excludes, then adds regex weights. It never calls an AI model. `score >= 30` means queued; it does not mean 30/100.
3. Geography is represented by `Job.is_remote`, free-text location, a search target, and hard-exclude regexes. There is no employer-country allowlist, work-authorization object, contractor/payroll model, or unknown/manual-review status.
4. `jobs` has URL-derived primary key and a non-unique fingerprint index. Dedupe is a pre-insert query, not a database uniqueness invariant.
5. Operational visibility is limited to run totals, source call/found/error totals, filter-reason aggregates, and a zero-results watchdog. There are no metrics, traces, evaluation-cost counters, or latency histograms.

## Report index

- [01 — System overview](01-system-overview.md)
- [02 — End-to-end pipeline](02-end-to-end-pipeline.md)
- [03 — Matching and AI evaluation](03-matching-and-ai-evaluation.md)
- [04 — Geographic eligibility](04-geographic-eligibility.md)
- [05 — Data and persistence](05-data-and-persistence.md)
- [06 — Reliability, performance, security](06-reliability-performance-security.md)
- [07 — Testing and observability](07-testing-and-observability.md)
- [08 — Architecture review](08-architecture-review.md)
- [09 — Findings and roadmap](09-findings-and-roadmap.md)
- [10 — Architecture diagrams](10-architecture-diagrams.md)
- [Evidence index](evidence-index.md)

## Evidence approach and limits

Claims are grounded in checked-in source and current configuration, not README descriptions alone. The existing SQLite database was opened in SQLite read-only mode. Only aggregate counts, field null counts, and source-run error summaries were queried; no job descriptions or secrets were read. These figures describe this local snapshot, not a general production trend. Runtime performance, actual deployment state, source terms compliance, and legal eligibility are not established by repository inspection.

Graphify was available and used. The refreshed graph reported 191 nodes, 469 edges, and 10 communities; its call edges place `run_once` at the center of the application-to-pipeline flow. Graph output is generated evidence, not a substitute for source review. Graphify's installed package is older than the skill bundle, and it reported unsupported files skipped.
