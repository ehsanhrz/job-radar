# 10 — Architecture diagrams

## Current production service

```mermaid
flowchart LR
  CLI[python -m jobradar service] --> S[run_service]
  S --> B[One Telegram polling thread]
  S --> C[RunCoordinator]
  B -->|/run| C
  C -->|scheduled or manual, never overlapping| P[run_once]
  DB[(SQLite)] -->|meta.last_run_at| S
  P -->|start time / finish time| DB
  P --> COL[Collectors + ATS boards]
  COL --> M[Matching Engine V2]
  M --> DB
  DB --> Q[V2 queued and review jobs]
  Q --> TG[Telegram delivery]
  TG --> B
```

`last_run_at` is written before a non-dry run. A fresh database schedules the
first run immediately. A normal service restart calculates the remaining
configured interval from that value and does not immediately repeat the job
search. `last_run_finished_at` records successful completion; a missing/older
finish timestamp remains diagnostic evidence of an interrupted run.

## V2 evaluation and persistence

```mermaid
flowchart TD
  D[Normalized Job] --> R[Role classification]
  D --> X[Requirement extraction]
  CP[candidate_profile.yaml] --> SM[Skill matching]
  R --> SC[Role-aware scoring]
  X --> SM
  X --> SE[Seniority evaluation]
  SM --> SC
  SE --> SC
  SC --> E[TechnicalEvaluation
score 0-100 + confidence + evidence]
  E --> J[(jobs: additive V2 columns + JSON details)]
  E -->|strong/good| Q[queued]
  E -->|possible/insufficient information| RV[/review]
  E -->|poor or separate geographic exclusion| F[filtered]
```

The technical score is separate from geographic/work-authorization eligibility
and from user application status.

## AI boundary

```mermaid
flowchart LR
  A[ai.py legacy optional reviewer] -. not invoked by V2 .-> P[run_once]
  V2[Matching Engine V2
deterministic extraction] --> P
  A --> API[OpenAI-compatible API when explicitly integrated]
```

`ai.py` parses constrained JSON, validates quoted infeasibility evidence, and
has rate-limit/failure circuit-breaker handling. Current V2 production runs do
not call it or combine its old score with normalized technical fit.

## Current database model

```mermaid
erDiagram
  JOBS {
    string id PK
    string fingerprint
    integer score
    integer evaluation_version
    integer technical_fit_score
    string role_family
    string seniority
    float confidence
    string match_decision
    json evaluation_details
    string status
    integer tg_message_id
  }
  SOURCE_RUNS {
    integer id PK
    string run_at
    string source
    integer calls
    integer found
    integer errors
  }
  RUNS {
    integer id PK
    string started_at
    string finished_at
    string trigger
  }
  META {
    string key PK
    string value
  }
```

No declared foreign-key relationships exist. `meta` stores Telegram offsets,
watchdog state, and scheduling timestamps.
