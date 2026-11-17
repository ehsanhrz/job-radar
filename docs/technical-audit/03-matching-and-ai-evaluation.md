# 03 — Matching and AI evaluation

## Active technical evaluation: Matching Engine V2

`pipeline.run_once()` constructs `MatchingEngine` and uses it for new jobs and
pending legacy `queued`/`low` records. The result is a typed
`TechnicalEvaluation` in `jobradar/matching.py`, persisted with
`evaluation_version=2`. It separates technical fit from geographic eligibility.

The active stages are:

1. Classify title and description into a role family, retaining confidence,
   secondary roles, responsibilities, and supporting source text.
2. Extract deduplicated mandatory, preferred, optional, responsibility,
   environment, and explicitly-not-required technologies. Known `or` groups
   are represented as alternatives rather than two separate gaps.
3. Match each requirement against the versioned candidate profile using direct,
   transferable, partial, missing, unknown, and not-applicable outcomes.
4. Infer seniority from title, years, and ownership/leadership expectations.
5. Produce a bounded 0–100 technical score, separate 0–1 confidence, decision,
   score breakdown, evidence, reasons, and gaps.

The candidate facts are configurable in `candidate_profile.yaml`; explicit
limitations prevent AWS, Azure, GCP, production Kubernetes, SRE, and dedicated
DevOps experience from being overstated. Matching configuration supplies the
weights and thresholds. The score is a renormalized weighted combination of
role compatibility (25), mandatory coverage (40), preferred coverage (12),
responsibilities (10), seniority (8), practices (3), and domain evidence (2).
Explicit mandatory cloud/infrastructure gaps can add the configured capped
deduction. Unknown information is not converted into a missing skill.

V2 is deterministic text-rule extraction, not semantic matching. It uses a
known technology lexicon and sentence-level wording such as “required”,
“preferred”, “plus”, negation, and alternatives. Unfamiliar wording, implicit
requirements, and long cross-sentence qualifications may remain uncertain or
be misclassified. Stored evidence is source text, not invented quotations.

## Delivery and legacy compatibility

Strong and good V2 decisions are queued; possible and insufficient-information
results remain in the Telegram `/review` queue; poor technical matches are
filtered. Telegram’s compact job rendering shows fit, role, seniority,
confidence, strong reasons, and important gaps. `python -m jobradar explain
JOB_ID` prints the full persisted explanation.

Legacy `Evaluator` regex scoring remains in `jobradar/filters.py` for callers
that still use it, but it is not the normal production ingestion evaluator.
Legacy unbounded `score` values are retained as history and are not combined
with normalized V2 scores for delivery. Previously sent, applied, saved,
skipped, and filtered records are not silently reevaluated or requeued.

## `ai.py`: optional legacy reviewer

`jobradar/ai.py` is present and should not be described as missing. It implements
an optional OpenAI-compatible HTTP reviewer using `requests` and the environment
variables `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, and optional `AI_PROFILE`.
It asks for a constrained JSON review containing fit, feasibility, evidence,
company-HQ claim, reason, and flags. It clamps/parses response fields, validates
an infeasibility quote against the posting before using it as evidence, spaces
requests, retries provider-specific extra parameters once, and disables itself
for the remainder of a run after consecutive failures or rejected credentials.

The current V2 pipeline intentionally does **not** call `AIReviewer` or
`apply_review`. `run_once()` reports the legacy AI path as disabled for Matching
V2 so old AI-adjusted unbounded scores cannot be mixed with V2 fit. The module is
therefore legacy capability, not an active semantic evaluation stage. If it is
reactivated, the US-company veto deserves special review because it can rely on
a model claim without source-text evidence; V2 currently avoids that path.

## Remaining geographic boundary

Existing geographic and work-authorization exclusion behavior remains outside
V2 as requested. A high technical fit is not a claim that a role can hire the
candidate from Iran. Those rules should eventually have their own evidence and
review model instead of being inferred from an AI response or folded into fit.
