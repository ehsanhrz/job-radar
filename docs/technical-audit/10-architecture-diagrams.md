# 10 — Architecture diagrams

All diagrams labeled **current** reflect inspected code. The final diagram is explicitly proposed.

## Current high-level architecture

```mermaid
flowchart LR
  CLI[CLI: run / loop / stats / rescore] --> P[pipeline.run_once]
  LOOP[Docker worker loop] --> P
  BOT[Telegram bot long poll] --> DB[(SQLite)]
  BOT -->|/run thread| P
  P --> COL[collect_all]
  COL --> JS[JobSpy collector]
  COL --> AN[Arbeitnow API collector]
  JS --> NORM[Job normalization]
  AN --> NORM
  NORM --> EVAL[Regex Evaluator]
  EVAL --> DB
  P --> DB
  DB --> Q[Queued jobs]
  Q --> TG[Telegram Bot API]
  TG --> USER[Single configured Telegram chat]
  USER -->|Applied / Skip / Save callback| BOT
  P --> WD[Zero-results watchdog]
  WD --> TG
```

## Current job lifecycle

```mermaid
flowchart TD
  A[Configured targets and search terms] --> B[JobSpy serial target × term × site calls]
  A --> C[Arbeitnow paginated API, up to configured pages]
  B --> D[Normalize to Job]
  C --> D
  D --> E{URL ID or company/title/city fingerprint already stored?}
  E -->|yes| F[Skip as duplicate]
  E -->|no| G[Evaluator.evaluate]
  G --> H{Title include / exclude / hard exclude / age}
  H -->|fail| I[Persist filtered + first filter reason]
  H -->|pass| J[Add configured regex weights and penalties]
  J --> K{Score >= min_score?}
  K -->|no| L[Persist low]
  K -->|yes| M[Persist queued]
  M --> N[Read top queued by score]
  N --> O{dry run or Telegram send succeeds?}
  O -->|dry run| P[Print message; keep queued]
  O -->|send succeeds| Q[Persist sent and Telegram message ID]
  O -->|send fails| R[Stop delivery loop; remainder stays queued]
  Q --> S[User callback updates sent/applied/skipped/saved]
```

## Current source integrations

```mermaid
flowchart LR
  CFG[config.yaml targets, terms, scrape options] --> JSC[JobSpy collector]
  JSC --> LI[LinkedIn via JobSpy]
  JSC --> IND[Indeed via JobSpy]
  JSC --> BAYT[Bayt via JobSpy]
  JSC --> GLASS[Glassdoor supported by adapter, not configured]
  LI --> JRES[CollectResult jobs + source health]
  IND --> JRES
  BAYT --> JRES
  GLASS --> JRES
  CFG --> AC[Arbeitnow collector]
  AC --> API[Official JSON endpoint, links.next pagination]
  API --> ARES[CollectResult jobs + source health]
  JRES --> PIPE[pipeline]
  ARES --> PIPE
  SCRIPT[script.py Jaabz fetch for offline HTML inspection] -. not imported by runtime .-> NONE[No Jaabz collector]
```

The source names from the user-provided source list that are not in the diagram have no active adapter in this checkout.

## Current Telegram callback sequence

```mermaid
sequenceDiagram
  participant W as Worker/pipeline
  participant DB as SQLite
  participant TG as Telegram API
  participant U as User
  W->>DB: SELECT queued ORDER BY score DESC
  W->>TG: sendMessage with buttons
  TG-->>W: message_id
  W->>DB: status=sent, sent_at, tg_message_id
  U->>TG: press Applied / Skip / Save
  TG-->>W: callback_query (bot long poll or next run drain)
  W->>DB: set_status(job_id, callback status)
  W->>TG: edit button state + answer callback
```

Current implementation does not persist Telegram delivery attempts separately, and callback offsets advance after handler exceptions.

## Current database ER model

```mermaid
erDiagram
  JOBS {
    string id PK
    string fingerprint
    string source
    string site
    string target
    string title
    string company
    string location
    string url
    integer is_remote
    string date_posted
    string salary
    string description
    integer score
    json reasons
    string status
    string filter_reason
    string first_seen
    string sent_at
    integer tg_message_id
    string updated_at
  }
  SOURCE_RUNS {
    integer id PK
    string run_at
    string source
    integer calls
    integer found
    integer errors
    string last_error
  }
  META {
    string key PK
    string value
  }
```

No declared foreign-key relationships exist among these tables.

## Current worker concurrency

```mermaid
flowchart TD
  subgraph Compose
    W[Worker process: scheduled run_once]
    B[Bot process: long poll]
    R[Bot daemon thread: /run]
    DB[(shared SQLite WAL file)]
  end
  B --> R
  W --> DB
  R --> DB
  B --> DB
  W -. no shared run lock .- R
  W -. SELECT seen then INSERT is not atomic for fingerprint .- DB
```

## Proposed conceptual evaluation flow (future)

```mermaid
flowchart TD
  D[Discovery] --> N[Normalization + preserve source evidence]
  N --> U[URL and semantic dedupe]
  U --> V[Basic validation: title, URL, age, description quality]
  V --> T[Technical evaluation: role family, required/preferred skills, seniority, domain]
  T --> G[Geographic evaluation: arrangement + allowed locations + work authorization + evidence]
  G --> P{Eligibility}
  P -->|eligible| HI[High-priority queue if technical fit is strong]
  P -->|potentially eligible| MR[Manual review queue]
  P -->|unknown| MR
  P -->|ineligible with explicit evidence| RJ[Searchable rejected state + evidence]
  HI --> PR[Application priority: fit + feasibility + freshness + compensation]
  MR --> PR
  PR --> OUT[Explainable delivery]
```

No current AI evaluator or separate technical/geographic stages exist; this diagram is a recommendation, not current architecture.

## Proposed AI evaluation sequence (future, optional)

```mermaid
sequenceDiagram
  participant P as Pipeline
  participant T as Technical policy
  participant G as Geographic policy
  participant M as Optional model
  participant V as Strict schema validator
  participant DB as SQLite
  P->>T: normalized job + candidate profile
  T->>T: deterministic role family, hard requirements, preferred skills
  T->>M: job description as untrusted data + bounded extraction request
  M-->>V: structured facts, gaps, evidence, confidence
  V->>V: enum/type/range/evidence validation
  alt invalid output or timeout
    V-->>T: mark semantic extraction unavailable; keep uncertain facts unknown
  else valid output
    V-->>T: validated extracted requirements and evidence
  end
  T->>G: technical assessment + geographic evidence
  G->>G: evaluate arrangement and candidate-country eligibility
  G->>DB: persist separate fit, feasibility, priority, evidence, decision
```

The model in this proposed sequence cannot declare legal eligibility without supporting evidence and must not override explicit deterministic disqualifiers. Model provider, prompts, persistence, and costs do not exist in current code.

## Current versus proposed architecture

```mermaid
flowchart LR
  subgraph CURRENT[Current]
    C1[Collectors] --> C2[Single regex Evaluator]
    C2 --> C3[One weighted score]
    C3 --> C4[filtered / low / queued]
    C4 --> C5[Telegram]
  end
  subgraph FUTURE[Proposed]
    F1[Collectors + preserved evidence] --> F2[Basic validation + dedupe]
    F2 --> F3[Technical fit + mandatory/preferred gaps]
    F2 --> F4[Geographic arrangement + eligibility]
    F3 --> F5[Priority score]
    F4 --> F5
    F5 --> F6[Eligible delivery / review queue / searchable ineligible]
    F6 --> F7[Explainable Telegram presentation]
  end
```
