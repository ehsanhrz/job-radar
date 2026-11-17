# 04 — Geographic eligibility

## What the current system stores

`Job` contains a nullable boolean `is_remote`, one free-text `location`, full description, and `target` search label (`jobradar/models.py:22-57`). JobSpy converts its `is_remote` value to bool (`jobradar/collectors/jobspy_collector.py:60-72`); Arbeitnow does the same with its `remote` field (`jobradar/collectors/arbeitnow.py:31-42`). The DB persists only `is_remote` as an integer, location, target, and description (`jobradar/db.py:11-34`).

The evaluator awards a remote bonus if the boolean is true **or** the text contains the word “remote”, `uzaktan`, or `home office` (`jobradar/filters.py:86-90`). It has no categories for worldwide, country- or region-restricted remote, hybrid, onsite, probationary remote, relocation, sponsorship, contractor, EOR, payroll feasibility, or unknown. `target: Worldwide (remote)` is search provenance, not proof of employer eligibility.

## Actual policy effects and risks

1. **Remote means a bonus, not an eligibility decision.** “Remote - US”, “Remote - EU”, and “Remote worldwide” all receive essentially the same remote bonus if they contain a remote flag/word. The configured global-remote phrase bonus adds relevance points but does not verify allowed countries.
2. **Some restrictions are hard rejections.** The `hard_exclude` patterns include required residence/work authorization in the US/UK/EU/Canada/Europe, and sanctioned/embargoed country language. These are based on regex over the entire combined text, without preserving the matched evidence separately (`config.yaml:95-105`, `filters.py:65-70`). A vague, negated, or boilerplate mention may reject incorrectly; uncovered phrasing may pass.
3. **No Iran-specific reasoning.** No code checks whether Iran is allowed or disallowed by a specific employer, platform, payroll provider, or applicable compliance program. The prompt’s candidate location is not modeled in config or code.
4. **Relocation and work arrangements are incomplete.** A job with visa sponsorship is not scored as a separate viable path. On-site/hybrid are penalties, not categorical blockers. Remote-after-probation and mandatory office visits are indistinguishable from generic text.
5. **Unknown does not exist as a workflow state.** A job either becomes filtered, low, or queued. It cannot be retained in a manual-review queue based on uncertain geographic eligibility.

## Database snapshot evidence

In the existing local database, `is_remote` was null for 0/1,158 jobs (it is effectively normalized to false by the current collectors), true for only 88 jobs, and false for 1,070. Of 81 queued jobs, 8 have `is_remote=1` and 73 have `is_remote=0`. The description field is null on 28 jobs. These counts do not establish actual geographic feasibility; they show that the stored metadata cannot represent it. Local source snapshot date in records is 2026-11-01; the figures should not be generalized to future runs.

## Proposed eligibility model (not implemented)

Separate **arrangement** from **eligibility**. Arrangement: `worldwide_remote`, `country_restricted_remote`, `region_restricted_remote`, `hybrid`, `onsite`, `remote_after_probation`, `relocation_available`, `relocation_with_sponsorship`, `contractor_supported`, `unknown`. Eligibility: `eligible`, `potentially_eligible`, `ineligible`, `unknown`. Store allowed countries/regions, candidate residency requirements, work authorization, visa/relocation terms, contract type/EOR/payroll limitation, and compliance statement only when sourced. Every nontrivial decision should retain an evidence snippet, source location, retrieval time, and confidence.

For Iran-related assessment, do not infer company policy from company nationality or an unspecific “worldwide” claim. Explicit employer text can establish a stated restriction or permission. Other legal/compliance concerns stay unknown/manual verification unless backed by reliable, current, relevant evidence. Unknown eligibility should be reviewable; confirmed ineligible should be excluded from application delivery; potentially eligible should be prioritized for manual verification.

## Candidate-specific conclusion

The current implementation cannot establish if a remote job can engage a person physically resident in Iran. It also cannot distinguish contractor feasibility from employee payroll eligibility. It should be treated as a stack-keyword collector, not a geographic feasibility filter. No legal conclusion about sanctions or any individual employer is supported by this code or database.
