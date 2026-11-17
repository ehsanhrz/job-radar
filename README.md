# 🛰 Job Radar

Scrapes LinkedIn, Indeed, Bayt and Arbeitnow for backend / .NET roles that are
**realistic to get while living in Iran**, scores them, and sends the good ones to
your Telegram with **✅ Applied / ⏭ Skip / ⭐ Save** buttons.

```
collect (JobSpy + Arbeitnow API)
  → normalize → dedupe (URL + company/title/city fingerprint, across boards)
  → eligibility filters (location, work authorization, remote policy, age)
  → deterministic Matching Engine V2 (role, requirements, skills, seniority, fit and confidence)
  → SQLite → Telegram (one message per job + run summary)
  → watchdog: alerts you when a source silently dies (0 jobs N runs in a row)
```

Search and eligibility policy live in [`config.yaml`](config.yaml). The versioned,
editable candidate evidence and proficiency profile lives in
[`candidate_profile.yaml`](candidate_profile.yaml). Secrets live in `.env`.

Matching V2 classifies the role from both title and responsibilities, extracts
explicit required/preferred/optional technologies, and scores demonstrated direct
and transferable experience on a bounded 0–100 scale. Its confidence is separate
from fit. This is deterministic text-rule extraction, not semantic AI; descriptions
that use unusual wording may remain uncertain. Inspect a persisted explanation
with `python -m jobradar explain JOB_ID`, or use Telegram `/review [page]` to find
possible and low-confidence V2 matches.

### Matching V2 policy

The score is a weighted mean of active dimensions. Configured weights are role
compatibility 25%, mandatory skill coverage 40%, preferred coverage 12%, relevant
responsibilities 10%, seniority 8%, engineering practices 3%, and domain fit 2%.
Dimensions with no extracted evidence are omitted and the remaining weights are
renormalized. Mandatory skill-match values are 1.00 for demonstrated primary or
strong professional experience, 0.62 for configured transferable experience,
0.42 for partial experience, and 0 for missing experience. Unknown requirements
are excluded from the coverage denominator rather than called missing. A mandatory
AWS, Azure, GCP, Kubernetes, or Terraform gap receives an additional configured
8 point deduction (capped at 18) because explicit infrastructure ownership is
material to those roles. The complete dimensions, active weights, and deduction
are stored with each explanation.

Role classification scores title evidence and responsibility/description evidence
separately. Strong SRE, platform, and DevOps responsibilities reduce generic
backend evidence. Weak or tied signals remain `unknown`. The candidate's five
years are evaluated against title, stated experience, and ownership expectations;
the candidate profile intentionally does not claim years for individual tools.

Decisions use configurable thresholds: `strong_match` requires at least 80 fit
and 0.60 confidence; `good_match` starts at 65; `possible_match` starts at 45;
below that is `poor_match`. Confidence below 0.25 yields
`insufficient_information` regardless of fit. Strong/good results are queued;
possible and insufficient-information results remain in `/review`; poor technical
matches are filtered. Geography and work authorization remain separate eligibility
checks.

Confidence is calculated separately as `0.22 + min(unique description words / 400,
0.28) + min(requirement groups / 8, 0.22) + min(responsibility sentences / 6,
0.16) + role confidence * 0.16`, capped to 0.05–0.98. With no description it is
capped at 0.25; a mandatory sentence whose skill could not be extracted subtracts
0.08. The input counts and caps are persisted in the scoring breakdown.

Requirement extraction is deliberately conservative and deterministic. It uses a
known technology lexicon, sentence-level cues such as “required”, “preferred”,
“a plus”, negation phrases, and explicit `or`/recognized slash alternatives.
Environment mentions are not treated as candidate requirements. Unusual wording,
long cross-sentence qualifications, implicit requirements, and unfamiliar tools
can remain unknown or be misclassified; the source sentence and extraction result
are persisted for review. No LLM or AI provider is used to set V2 fit. The legacy
`ai.enabled` settings do not alter V2 scores.

---

## 1. Telegram setup (2 minutes)

1. In Telegram, open **@BotFather** → `/newbot` → pick a name → copy the token.
2. `cp .env.example .env` and paste it into `TELEGRAM_BOT_TOKEN`.
3. Open your new bot and send it `/start`.
4. Get your chat id and put it in `TELEGRAM_CHAT_ID`:
   ```bash
   python -m jobradar chat-id
   ```
5. Test it:
   ```bash
   python -m jobradar test-telegram
   ```

> The bot **only** answers the chat id in `.env`. Anyone else is ignored.

## 2. Run locally (morning mode)

Requires Python 3.10+ and your VPN on (LinkedIn/Indeed are blocked or geo-fenced from Iran).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m jobradar run --dry-run   # first time: print matches, send nothing
python -m jobradar run             # the real thing
```

A run is ~30 searches with polite random pauses → usually **5–10 minutes**.
It also processes any button taps you made since the last run (Telegram keeps
them ~24h), so a once-a-morning run is enough. Jobs that didn't fit in
`max_per_run` stay queued for the next run.

### VPN notes

- Easiest: put your VPN client in **TUN / system-wide mode**.
- If your client only exposes a local proxy (v2rayN etc. → `127.0.0.1:10808` socks /
  `10809` http), set both of these in `.env`:
  ```
  TELEGRAM_PROXY=socks5h://127.0.0.1:10808
  SCRAPE_PROXIES=127.0.0.1:10809
  ```

## 3. Production deployment (one container)

Build one image and run one Python process. It starts one Telegram polling loop
and one coordinated scheduler. A fresh database runs its first search at startup;
later service restarts use SQLite's persisted last-run time and wait for the
remaining configured schedule instead of immediately searching again. If a run
was killed mid-execution (for example by a redeploy), a one-off slot schedules a
retry about five minutes after restart. The scheduler uses
`schedule.interval_hours` and `schedule.jitter_minutes` from `config.yaml`.
Create `.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
any optional proxy settings before starting. Secrets are supplied at runtime,
not copied into the image.

```bash
docker build -t jobradar:latest .
docker volume create jobradar-data
docker run -d --name jobradar --restart unless-stopped \
  --env-file .env \
  -v jobradar-data:/app/data \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  jobradar:latest
```

The default image command is `python -m jobradar service`. The database lives at
`/app/data/jobradar.db` and persists in the `jobradar-data` volume. Back up that
volume off-host. Runtime configuration can be changed by editing the mounted
`config.yaml` and restarting the container.

```bash
docker logs -f jobradar                 # follow stdout/stderr
docker restart jobradar                 # restart the service
docker stop jobradar                    # SIGTERM; wait for clean shutdown
docker start jobradar
```

Docker sends SIGTERM on stop; the service stops scheduling, lets an active run
finish, and waits for Telegram's long poll to return before exiting. SIGINT is
also handled. `docker-compose.yml` remains an optional local development helper;
production does not require Compose.

## 4. Telegram commands

| Command    | What it does |
|------------|--------------|
| `/stats`   | counts by status, source health, top filter reasons |
| `/saved`   | your ⭐ jobs |
| `/applied [page]` | applied job list with total and pagination (text and inline buttons) |
| `/review [page]`  | possible/low-confidence V2 matches with a score and explanation id |
| `/vetoed`  | jobs an AI review dropped, with the quoted reason |
| `/rescore` | re-apply current rules to unsent jobs and send new matches |
| `/report [24h\|7d\|all]` | full JSON report (runs, sources, issues, jobs) as a file |
| `/companies` | discovered company ATS boards and their watch status |
| `/run`     | scrape now (always-on mode only; cooldown is 30 min after a finished run) |
| `/run force` | scrape now even inside the cooldown |

Every job message has **✅ Applied / ⏭ Skip / ⭐ Save**, plus **↩️ Undo** after you tap one.

## 5. Tuning

```bash
python -m jobradar stats     # what got filtered and why, source health
# edit config.yaml or candidate_profile.yaml
python -m jobradar rescore   # reevaluate pending jobs; sent/application history is preserved
python -m jobradar run
```

Tips:
- **Too much noise?** Raise `matching.thresholds.good_match` / `strong_match`, or improve
  the candidate profile's proficiency and evidence descriptions. Title patterns are
  retained for legacy evaluator callers but do not gate production V2 discovery.
- **Missing good jobs?** Check `stats` → "top filter reasons". If `title: no include match`
  is huge, widen `filters.title_include`.
- Add a country: copy a block in `targets`. Indeed needs `country_indeed`
  (e.g. `qatar`, `saudi arabia`, `romania`); LinkedIn/Bayt just use `location`.

## 6. When a source gets blocked

The watchdog sends `⚠️ linkedin returned 0 jobs in the last 3 runs` and
`✅ linkedin is back` when it recovers. LinkedIn and Indeed dislike datacenter and
shared-VPN IPs. In order of effort:

1. Do nothing for a day. Rate limits are often temporary.
2. Lower `scrape.results_per_call` and/or raise `delay_between_calls_sec`.
3. Put a **residential proxy** in `SCRAPE_PROXIES`. Traffic is tiny, so the cheapest plan is enough.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Roadmap

- [x] **Phase 1**: JobSpy (LinkedIn, Indeed, Bayt) + Arbeitnow → filters/scoring → Telegram, watchdog
- [ ] **Phase 2**: Kariyer.net scraper (Turkish market), more Gulf boards
- [ ] **Phase 3**: optional LLM pass ("realistic from Iran?", salary/remote extraction)
- [ ] **Phase 4**: weekly stats, quick tailored cover-letter drafts per job

## Disclaimer

For personal job hunting. Scraping may violate some sites' terms of service.
**Never run it logged in with your main LinkedIn account.** JobSpy uses LinkedIn's
public, logged-out pages. Keep volumes low.
