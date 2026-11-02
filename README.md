# 🛰 Job Radar

A personal job scout. It searches LinkedIn, Indeed, remote job boards, free job feeds and
companies' own hiring pages several times a day. It drops what you can't realistically get,
scores the rest against your skills, optionally asks a free AI for a second opinion, and sends
the good ones to your Telegram with **✅ Applied / ⏭ Skip / ⭐ Save** buttons.

It was built for a senior .NET backend engineer living in Iran who can only work remotely,
so the default `config.yaml` is tuned for that: no US employers (sanctions), no
"remote within Germany only", no German-required postings, no junior roles. **Every one of
those rules is a line in `config.yaml`.** Section 6 shows how to make it yours.

```
collect   LinkedIn, Indeed (JobSpy) · Arbeitnow, Himalayas, Jobicy, Remotive (APIs)
          Jaabz (HTML) · HN "Who is hiring", RemoteOK, We Work Remotely (feeds)
          company boards: Greenhouse, Lever, Ashby, Workable (auto-discovered)
   → dedupe        same URL, or same company + title + city across boards
   → hard filters  location, residency/citizenship demands, junior, wrong stack…
   → score         your keywords ⬆, remote ⬆, on-site / foreign language ⬇ …
   → AI review     fit 0–10, red flags, vetoes (optional, free key, fail-safe)
   → Telegram      one message per job + a run summary
   → watchdog      tells you when a source silently dies
```

**Contents**

1. [Quick start](#1-quick-start)
2. [Telegram bot](#2-telegram-bot)
3. [Free AI key (Groq)](#3-free-ai-key-groq)
4. [Hosting](#4-hosting)
5. [Daily use](#5-daily-use)
6. [Make it yours: the tuning guide](#6-make-it-yours-the-tuning-guide)
7. [The weekly tuning loop (reports)](#7-the-weekly-tuning-loop-reports)
8. [When a source gets blocked](#8-when-a-source-gets-blocked)
9. [Reference: environment variables](#9-reference-environment-variables)
10. [Troubleshooting](#10-troubleshooting)
11. [Development](#11-development)

---

## 1. Quick start

```bash
git clone https://github.com/ehsanhrz/job-radar.git && cd job-radar
cp .env.example .env            # fill in TELEGRAM_BOT_TOKEN (section 2)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m jobradar chat-id      # after sending /start to your bot -> paste into TELEGRAM_CHAT_ID
python -m jobradar test-telegram
python -m jobradar run --dry-run   # prints matches, sends nothing
python -m jobradar serve           # bot + scheduler, runs forever
```

Then, before you trust it:

- [ ] Rewrite `search_terms`, `targets` and `scoring.title_keywords` for **your** stack ([6.2](#62-what-to-search-and-where), [6.4](#64-scoring))
- [ ] Add an AI key and write your `AI_PROFILE` (section 3)
- [ ] Remove the rules that are only about Iran if they don't apply to you ([6.6](#66-recipes))
- [ ] Host it somewhere always-on with a persistent disk (section 4)

## 2. Telegram bot

1. In Telegram, open **@BotFather** → `/newbot` → pick a name and username → copy the token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Open your new bot and send it `/start`.
4. Run `python -m jobradar chat-id` and put the number in `TELEGRAM_CHAT_ID`.
5. `python -m jobradar test-telegram`. You should get a message.

**Privacy:** the bot ignores every chat except `TELEGRAM_CHAT_ID`, and inside it only you
(or the user ids in `TELEGRAM_ALLOWED_USER_IDS`, if you use a group). Strangers who find
your bot get no answer, and nothing they send is processed.

## 3. Free AI key (Groq)

Optional, but it's the difference between "keyword match" and "actually fits you". Without
a key everything still works on keyword scores.

### 3.1 Get the key

1. Sign up at **[console.groq.com](https://console.groq.com)** (Google/GitHub/email). No credit card for the free plan.
2. Go to **[API Keys](https://console.groq.com/keys)** → **Create API Key** → copy it (`gsk_…`). It's shown once.
3. In `.env` (or your host's env panel):
   ```
   AI_BASE_URL=https://api.groq.com/openai/v1
   AI_API_KEY=gsk_...
   AI_MODEL=openai/gpt-oss-120b
   AI_PROFILE=...   # see 3.2
   ```

> From Iran you'll need a VPN to sign up, and the bot has to run on a server outside Iran
> anyway (section 4). The key is only ever used from that server.

**Free-plan limits** for `openai/gpt-oss-120b` are currently about 30 requests/min,
1,000 requests/day, **8K tokens/min and 200K tokens/day**, per account (not per key, so
extra keys don't help). Check yours at
[console.groq.com/settings/limits](https://console.groq.com/settings/limits).
One review costs ~1.5K tokens, so the defaults in `config.yaml` (`max_reviews_per_run: 15`,
`min_seconds_between_calls: 15`) stay well inside that with 4 runs a day.

### 3.2 Write a good `AI_PROFILE`

This is what the AI judges every job against. It lives in an env var, not the repo, so your
details aren't published. 5–10 short lines, facts not adjectives:

```
Senior backend engineer, 5 years. Main stack C#/.NET 8, ASP.NET Core, EF Core, PostgreSQL,
SQL Server, Redis, RabbitMQ, Kafka. Also NestJS/TypeScript, MongoDB. Docker, self-hosted
devops, OpenTelemetry/Grafana. No AWS/Azure production experience.
Domain: fintech (trading systems, wallets, exchange and KYC integrations). DDD, CQRS,
event-driven, clean architecture.
Lives in Iran, can't relocate: remote only, contractor/B2B fine.
Languages: English (professional), Turkish (native), Persian (fluent). No German/Dutch/French.
Not interested in: junior roles, frontend-only, mobile, people management.
```

Tips: name what you **don't** have (the AI will flag jobs that need it), state your hard
constraints in plain words, and update it when your situation changes. A restart picks it up.

### 3.3 What the AI does with a job

For each job that passed the keyword filter (highest scores first, up to
`ai.max_reviews_per_run`) it returns:

| Field | Effect |
|---|---|
| **fit 0–10** | final score = keyword score + (fit − 5) × `ai.weight`. Fit ≤ `ai.veto_fit_at_or_below` → dropped as "poor fit" |
| **feasible** yes/no/unclear | "no" drops the job **only if the AI quotes the restricting sentence and that exact text is in the posting** (`ai.veto_infeasible`). Guesses can't veto |
| **us_company** yes/no/unclear + **hq** | "yes" drops it (`ai.veto_us_company`: US employers can't hire from sanctioned countries). Otherwise the card shows `🏢 HQ: …` |
| **red flags** | shown on the card, e.g. "needs AWS", "German C1" |

Card example: `🤖 8/10 · fintech .NET match · ✅ Iran-remote yes` / `🏢 HQ: Estonia`.
Everything dropped by the AI is listed in `/vetoed` with the reason, so you can catch a
wrong call.

**Fail-safe by design:** no key, wrong key, 401/403/429/5xx, timeouts, broken JSON, even a
bug in the AI code → the job keeps its keyword score and is sent normally. An auth error or 3
failures in a row switch the AI off for the rest of that run, and the run summary says why.

### 3.4 Other free providers

Any OpenAI-compatible API works. Change three variables:

| Provider | `AI_BASE_URL` | `AI_MODEL` |
|---|---|---|
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |
| OpenRouter | `https://openrouter.ai/api/v1` | any model id ending in `:free` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | a Flash model |
| Cerebras | `https://api.cerebras.ai/v1` | see their model list |

`ai.extra_params` in `config.yaml` holds Groq-specific options (low reasoning effort, JSON
mode). If a provider rejects them (HTTP 400) they're dropped automatically for the run; set
`extra_params: {}` to stop sending them at all.

## 4. Hosting

Telegram, LinkedIn and Groq need a server **outside Iran** (or wherever they're blocked for you).

### 4.1 Docker (VPS)

One container runs the Telegram bot (instant buttons, commands) **and** the scheduler.

```bash
docker build -t job-radar .
docker run -d --name job-radar --restart unless-stopped \
  --env-file .env -v $(pwd)/data:/app/data job-radar
```

### 4.2 PaaS with git auto-deploy

- Service type **Docker**, Dockerfile in the repo root, branch `main`.
- Set the env vars from `.env.example` in the platform's panel (section 9).
- **Attach a persistent disk at `/app/data`.** Without it your database (and every Applied
  tap) is wiped on each deploy. The `🟢 Job Radar started` message after each deploy shows
  how many jobs the DB holds, and warns `⚠️ Empty database` if it was wiped.
- If the platform requires an open port, set `PORT=8080`; `GET /` returns a small health JSON.
- Redeploys don't cause a scrape storm: the schedule continues from the last run stored in the DB.

### 4.3 Your own machine (morning mode)

`python -m jobradar run` once a day with your VPN on. A run takes ~10 minutes and also
processes the button taps you made since the last run (Telegram keeps them ~24h).
If your VPN client only exposes a local proxy (v2rayN etc.):

```
TELEGRAM_PROXY=socks5h://127.0.0.1:10808
SCRAPE_PROXIES=127.0.0.1:10809
```

## 5. Daily use

Every job arrives as one message: title, company, location, score and *why* (the matched
keywords and penalties), the AI's verdict, and the link. Tap:

- **✅ Applied**: goes into `/applied`, your application log
- **⏭ Skip**: not for you (this is data too: the weekly report shows what you skip)
- **⭐ Save**: decide later, see `/saved`
- **↩️ Undo**: appears after any tap

| Command | What it does |
|---|---|
| `/stats` | counts by status, source health, top filter reasons |
| `/applied` · `/saved` | your lists |
| `/vetoed` | jobs the AI dropped, with its reason |
| `/companies` | company hiring boards discovered and watched |
| `/rescore` | re-apply the current rules to everything not yet sent, send new matches |
| `/report [24h\|7d\|all]` | full JSON report as a file (section 7) |
| `/run` | scrape now. Within 30 min of the last run it asks for `/run force` |

At most `telegram.max_per_run` jobs are sent per run, best first; the rest wait for the next run.

## 6. Make it yours: the tuning guide

### 6.1 Where things live

| What | Where | How to apply a change |
|---|---|---|
| Secrets, your profile | `.env` / host env vars | restart the container |
| Everything else: searches, sources, filters, weights | `config.yaml` | redeploy (git push), then `/rescore` |

Keeping your config private: copy `config.yaml` to your persistent disk (e.g.
`/app/data/config.yaml`) and set `JOBRADAR_CONFIG=/app/data/config.yaml`. Then edits don't
need a commit, just a restart.

All patterns in `config.yaml` are **case-insensitive Python regexes** in `'single quotes'`.
Cheat sheet:

| Pattern | Matches |
|---|---|
| `'back.?end'` | backend, back-end, back end |
| `'\b(senior\|sr)\b'` | senior, Sr (whole words only) |
| `'nest\.?js'` | nestjs, nest.js (`\.` = a literal dot) |
| `'(?<![a-z])\.net\b'` | .NET, but not the ".Net" in "Kariyer.Net" |
| `'^(?!.*python).*\bdjango\b'` | "django" only if "python" isn't in the same text |

Test a pattern quickly: `python -c "import re; print(re.search(r'back.?end', 'Back-End Dev', re.I))"`.

### 6.2 What to search, and where

```yaml
search_terms:                     # used for every target
  - '".NET" OR "C#"'              # LinkedIn/Indeed understand OR and quotes
  - '"backend developer" OR "backend engineer"'

targets:                          # one block per place
  - name: Worldwide (remote)
    sites: [linkedin]             # linkedin | indeed
    location: 'Worldwide'         # LinkedIn's location box
    is_remote: true               # LinkedIn/Indeed remote filter
    extra_search_terms: ['NestJS']
  - name: Turkey
    sites: [linkedin, indeed]
    location: 'Türkiye'
    country_indeed: turkey        # required for Indeed (turkey, uae, germany, uk, canada, ...)
```

- **Order matters:** LinkedIn usually rate-limits after a few searches and is skipped for
  the rest of the run, so put your most valuable target **first**.
- Every target × site × term is one search, so 7 targets × 4 terms is already ~50 searches.
  Fewer, sharper terms beat many vague ones.
- `scrape.hours_old` (default 72) and `results_per_call` (20) control how far back and how
  deep each search goes.

**Other sources** live under `extra_sources` and can each be switched off with `enabled: false`:

| Source | What it is | Notes |
|---|---|---|
| `arbeitnow` | Germany/EU + remote API | lots of on-site jobs; the remote filter handles them |
| `remote_boards` | Himalayas, Jobicy, Remotive APIs | own `search_terms` (plain words, `c#` breaks Jobicy) |
| `feeds` | HN "Who is hiring", RemoteOK, We Work Remotely | `wwr_rss` takes any WWR category feed URL |
| `jaabz` | international jobs, HTML | `list_urls` = any Jaabz search URL |
| `kariyer` | Kariyer.net (Turkey) | off by default: applying needs a Turkish phone |

Company boards (Greenhouse, Lever, Ashby, Workable) need no setup. Any job linking to one
reveals the company, and its board is then read directly once a day. See `ats:` in
`config.yaml` and [section 7](#company-boards-ats-watcher).

### 6.3 Hard filters (a match = the job is dropped)

| Key | Checked against | Default use |
|---|---|---|
| `title_include` | title must match **one** | your roles: .NET, backend, software engineer… |
| `title_exclude` | title | junior/intern, manager, mobile, QA, other stacks |
| `location_exclude` | location | US locations; "Remote: <region>" you can't work from |
| `hard_exclude` | whole posting | "must be authorized to work in…", clearance, "must reside in…" |
| `require_remote_sites` | sources whose remote flag is reliable | non-remote jobs from them are dropped |
| `direct_apply_only_sites` | sources you can't apply through | keep only postings linking to the company's site |
| `max_age_days` | posting date | 14 |

Every drop is stored with its reason, so `/stats` → "top filter reasons" tells you which rule
is eating your jobs.

### 6.4 Scoring

Each job starts at 0, gains or loses points, and is sent when it reaches `scoring.min_score`
(default **30**). The card lists every rule that fired, e.g. `title:.net +30 · postgres +5 ·
remote +10 · hybrid -10`, so you can see exactly why something scored what it did.

| Key | What it does |
|---|---|
| `title_keywords` | points when the **title** matches (your core stack should be big: `.NET` = 30) |
| `text_keywords` | points when the **description** matches (supporting tech, domain, architecture) |
| `penalties` | negative points: on-site, hybrid, "fluent German", "no sponsorship"… |
| `remote_bonus` / `not_remote_penalty` | remote flag or "remote" in the text / no sign of remote at all |
| `turkish_bonus` | posting written in Turkish (an edge if you speak it) |
| `foreign_language_penalty` | posting **written** in German/Dutch/French (not just "German is a plus") |
| `fresh_bonus` | posted in the last 2 days |
| `target_bonus` | per target name, e.g. `Turkey: 10` |
| `site_bonus` | per source, e.g. `wwr: -20` (a board that's mostly employers you can't work for) |

Rules of thumb:
- A perfect-stack title should clear `min_score` on its own; a generic "Software Engineer"
  title should need description evidence.
- Use **penalties** for "probably not", **hard filters** only for "definitely never".
- Change one thing at a time, then compare the next report.

### 6.5 Applying changes

```bash
python -m jobradar stats      # what got filtered and why, source health
# edit config.yaml
python -m jobradar rescore    # re-applies rules to unsent jobs (/rescore in Telegram also sends new matches)
```

On a PaaS: push the change, wait for the redeploy, send `/rescore`. Jobs already sent aren't
re-sent; jobs that now pass are sent; queued jobs that now fail are dropped.

### 6.6 Recipes

**Different stack (e.g. Java/Spring):**
1. `search_terms`: `'"Java" OR "Spring Boot"'`, `'"backend engineer"'`
2. `title_include`: replace the .NET line with `'\bjava\b|spring|kotlin'`
3. `title_exclude`: remove `java`/`kotlin` from the "other stacks" line, add `\.net|c#` if you like
4. `title_keywords`: `'\bjava\b|spring': 30`; `text_keywords`: `'spring boot': 10`, `'hibernate|jpa': 5`…
5. Rewrite `AI_PROFILE`.

**Not in a sanctioned country:**
- `filters.location_exclude`: delete the US line
- `filters.hard_exclude`: delete the `(sanctioned|embargoed) countr` line (and any you qualify for)
- `ai.veto_us_company: false`; `scoring.site_bonus: {}`
- `ats.skip_us_companies: false`
- In `AI_PROFILE` say where you live and which work authorizations you have.

**On-site / hybrid is fine (you can relocate or live there):**
- `filters.require_remote_sites: []`
- set the `on-site` / `hybrid` penalties to `0` (or delete them), `not_remote_penalty: 0`
- add targets for the cities you want.

**Too much noise:** raise `min_score` by 10, add the titles you keep skipping to
`title_exclude`, lower `telegram.max_per_run`.

**Too few jobs:** check `/stats` top filter reasons first. Then widen `title_include`, raise
`scrape.hours_old`, lower `min_score` by 10, add targets or search terms. Look at
`near_misses` in the report: those are the jobs just under the line.

**Save AI tokens:** lower `ai.max_reviews_per_run`; keep `review_margin: 0` so only jobs that
already passed are reviewed.

## 7. The weekly tuning loop (reports)

Once a week send `/report 7d`. You get a JSON file with:

- **runs**: trigger, start/end, duration, done/killed, per-source results, AI stats
- **sources**: health per source (`ok`, `degraded`, `empty`, `blocked_or_failing`) with the last error
- **issues**: auto-detected problems (blocked sources, killed runs, AI off or failing, backlog)
- **funnel**: found → new → filtered (by reason) → low → queued → sent
- **jobs**: sent, queued, applied/saved (all time), skipped, AI-vetoed, near misses, each with
  score, reasons, AI verdict, link and a description snippet
- **ats_companies**: company boards watched, what each one yielded, which were disabled and why

No tokens or keys are ever included. Read it yourself, or hand it to an AI assistant with
"what should I change in config.yaml?". What to look for:

| You see | Change |
|---|---|
| You skip most jobs from one source | `site_bonus` for it, or disable it |
| You skip a pattern of titles | add it to `title_exclude` |
| Good jobs in `near_misses` | raise the keywords they had, or lower `min_score` |
| Wrong AI vetoes | improve `AI_PROFILE`; or turn that veto off in `ai:` |
| A source `blocked_or_failing` for days | section 8, or disable it |
| Runs getting long | fewer targets/terms, lower `ats.max_companies_per_run` |

### Company boards (ATS watcher)

No company list to maintain. When a job links to `boards.greenhouse.io/<co>`,
`jobs.lever.co/<co>`, `jobs.ashbyhq.com/<co>` or `apply.workable.com/<co>`, that company is
saved and its board is read **directly** (public APIs, no keys, no anti-bot) at most once
every `ats.check_interval_hours`. That catches openings never posted on job boards, and you
apply on the company's own site.

Safety rails: at most `max_companies_per_run` boards per run; only postings that would
actually be sent are stored; 3 failures in a row or `disable_after_empty_checks` checks with
nothing relevant disable a board; a 429 pauses the watcher for the run; boards where most
postings (`us_share`) are in the US/North America are skipped; any error in this step leaves
the rest of the run untouched. `/companies` shows what's watched.

## 8. When a source gets blocked

The watchdog sends `⚠️ linkedin returned 0 jobs in the last 3 runs`, and `✅ linkedin is back`
when it recovers. LinkedIn and Indeed dislike datacenter and shared-VPN IPs.

1. Do nothing for a day. Most blocks (HTTP 429) are temporary.
2. Lower `scrape.results_per_call` / `linkedin_max_details`, raise the delays.
3. Put a **residential proxy** in `SCRAPE_PROXIES`. Traffic is tiny; the cheapest plan is enough.

`python -m jobradar probe` checks what the HTML boards and feeds return from the current machine.

**Never run it logged in with your main LinkedIn account.** It uses LinkedIn's public,
logged-out pages only.

## 9. Reference: environment variables

| Variable | Required | What |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | `python -m jobradar chat-id` |
| `TELEGRAM_ALLOWED_USER_IDS` | no | comma-separated user ids, only if the chat is a group |
| `TELEGRAM_PROXY` | no | proxy for Telegram calls, e.g. `socks5h://127.0.0.1:10808` |
| `SCRAPE_PROXIES` | no | comma-separated proxies for LinkedIn/Indeed: `host:port` or `user:pass@host:port` |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | no | AI provider (section 3). Empty key = AI off |
| `AI_PROFILE` | no | a few lines about you (section 3.2) |
| `JOBRADAR_CONFIG` | no | config path, default `config.yaml` |
| `JOBRADAR_DB` | no | database path, default `data/jobradar.db` |
| `PORT` | no | if set, serves a health JSON on `GET /` |

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No messages at all | `python -m jobradar test-telegram`; check the token and chat id; the server must reach Telegram |
| `⚠️ Empty database` after every deploy, `/applied` empty | no persistent disk at `/app/data` (4.2) |
| `/run` says wait or use `/run force` | a run finished less than 30 min ago; protects you from blocks |
| Run summary: `AI review failed … not valid JSON` | reasoning model ran out of tokens: keep `reasoning_effort: low`, raise `ai.max_tokens` |
| `AI off: key rejected (HTTP 401/403)` | wrong or revoked key, or the provider blocks the server's region |
| `AI … HTTP 429` | daily/minute limit: lower `max_reviews_per_run`, raise `min_seconds_between_calls` |
| `linkedin: 429` in the summary | normal now and then; section 8 if it lasts for days |
| A job you'd want got filtered | `/stats` top filter reasons, or search the report for it: `filter_reason` says which rule |
| A bad job keeps getting through | look at its reasons on the card, then add a penalty or exclude (6.3, 6.4) |

## 11. Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

```
jobradar/
  collectors/   one module per source; each returns jobs + per-call health, never raises
  filters.py    hard filters + scoring (pure, driven by config.yaml)
  ai.py         AI reviewer: OpenAI-compatible, circuit breaker, evidence-gated vetoes
  pipeline.py   collect → ingest → AI → send → watchdog
  bot.py        Telegram commands and buttons
  serve.py      bot + scheduler + health endpoint (the container's command)
  report.py     /report JSON
  db.py         SQLite (WAL) with in-place migrations
```

New source: add a collector returning `CollectResult`, register it in
`collectors/__init__.py`, add an `extra_sources` block and a test with a saved fixture.
Collectors must never raise: a broken source is reported, the run continues.

## Disclaimer

For personal job hunting. Scraping may violate some sites' terms of service; keep volumes
low and be polite. Nothing here is legal advice about sanctions or work authorization.
