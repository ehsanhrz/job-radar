# 03 — Matching and AI evaluation

## Actual evaluator

`Evaluator` compiles the configured title include/exclude patterns, hard excludes, scoring keyword weights, penalties, minimum score, and optional bonus settings (`jobradar/filters.py:37-59`). For each job, `evaluate()` executes in this order (`jobradar/filters.py:61-103`):

1. Require a title include match if include patterns exist.
2. Reject on the first title-exclude match.
3. Reject on the first `hard_exclude` match against concatenated title, company, location, and description.
4. Reject jobs older than `max_age_days` if a date exists.
5. Add every matching title weight, every matching text weight, and every matching penalty.
6. Add remote, Turkish-language, freshness, and target bonuses when applicable.
7. Mark `queued` when the resulting integer score reaches `min_score`; otherwise mark `low`.

This is deterministic regex scoring. It is not an AI pipeline. There is no model configuration, provider client, candidate-profile prompt, structured output, confidence, token/cost accounting, or AI decision persistence. README “Phase 3” explicitly lists the LLM pass as not yet implemented (`README.md:123-128`).

## Actual configured rules

- Titles must mention .NET/C#, backend/NestJS/Node/full-stack, software engineer/developer, or Turkish equivalents (`config.yaml:77-80`).
- Exclusions include intern/trainee, junior, director/manager, mobile, QA, selected enterprise stacks, and stack-specific Java/PHP/Ruby/Go/Python/React patterns (`config.yaml:82-93`). Some negative-lookahead exceptions preserve mixed-stack titles.
- Hard excludes cover work authorization/residency in selected geographies, citizenship, clearance, green cards, W-2, work permits, and sanctioned/embargoed wording (`config.yaml:95-105`). A regex hit drops the job, not sends it to review.
- Keyword scores include .NET (+30 title; +10 text), C# (+20 title; +10 text), seniority (+10), backend (+10), related databases/messaging/Docker (+5), integration (+3), remote-global terms (+15), and contractor words (+5). Several penalties are also configured (`config.yaml:108-138`).
- `min_score: 30`; no upper bound or normalization exists (`config.yaml:108`). Multiple keyword hits can exceed 100. Database evidence: sent jobs have average stored score 92.6 and max 118.

## Match-quality assessment for the stated candidate

**Strengths recognized:** C#/.NET, backend title, senior label, SQL Server, Mongo, microservice, Docker/Kubernetes, RabbitMQ/Kafka/Redis, integration, NestJS/TypeScript, Turkish posting, and selected language constraints have regex rules.

**Important profile experience not represented:** PostgreSQL, CQRS, DDD, Clean Architecture, event-driven systems, distributed systems, fintech/trading/payments/wallets/KYC, OpenTelemetry, Prometheus/Grafana/Loki/Tempo, Docker Swarm, Traefik, PgBouncer, production CI/CD, Linux, and roughly five years of experience are not meaningfully scored by the supplied rules. “Senior” is just a title substring, and there is no explicit years-of-experience test.

**Cloud/SRE distinction:** The config has one combined `docker|kubernetes|k8s` +5 pattern. It cannot tell an ordinary backend job from an SRE/Platform job and cannot distinguish required from preferred technologies. AWS/Azure/GCP requirements do not have specific logic. It neither evaluates transferability nor records skill gaps. Consequently, the system can reward a Kubernetes mention regardless of role, while failing to make the candidate’s actual Linux/observability/deployment experience visible.

**Keyword overreach:** A keyword hit anywhere in the description adds its full weight once, regardless of negation, context, requirement status, or whether the employer is describing the company stack. Repeated distinct configured rules all stack. In the local DB, 930 of 963 filtered jobs were rejected solely because the title matched no include rule, showing that description-level fit cannot rescue a potentially relevant job with a nonstandard title.

The title exclusion rules also reject standalone Go roles unless the title includes .NET/C# exceptions (`config.yaml:91-93`), despite Go being listed as some candidate experience in the supplied profile. No explicit Go skill score is configured.

## Decision explainability

`Verdict` stores one integer score, a list of matched keyword/penalty strings, status, and one filter reason (`jobradar/filters.py:30-35`). It cannot explain separate technical/geographic factors. `format_job()` shows score and up to six reason strings (`jobradar/telegram.py:111-127`), but filtered records show only the first matching filter reason. No confidence or evidence spans are stored.

## Recommended future evaluation contract (proposed)

Use schema-validated output with separate `technical_fit_score`, `geographic_feasibility_score`, and `application_priority_score`; controlled enums for arrangement and eligibility; booleans/unknowns for relocation, sponsorship, and contractor support; lists for mandatory gaps, preferred gaps, transferable skills, and rejection reasons; evidence quotes plus source/URL; and confidence. Keep deterministic hard constraints outside model discretion. Treat job text as untrusted data and instruct the model to extract facts rather than follow instructions in postings. Missing facts must remain unknown. No such schema exists currently.
