# 🛰 Job Radar

Scrapes LinkedIn, Indeed, Bayt and Arbeitnow for backend / .NET roles that are
**realistic to get while living in Iran**, scores them, and sends the good ones to
your Telegram with **✅ Applied / ⏭ Skip / ⭐ Save** buttons.

```
collect (JobSpy + Arbeitnow API)
  → normalize → dedupe (URL + company/title/city fingerprint, across boards)
  → hard filters (US-only, "must reside in EU", clearance, junior, other stacks…)
  → score (.NET/C# ⬆, Turkish-language posting ⬆, remote ⬆, German-required ⬇ …)
  → SQLite → Telegram (one message per job + run summary)
  → watchdog: alerts you when a source silently dies (0 jobs N runs in a row)
```

Everything you'd want to tune — countries, search terms, filters, weights — is in
[`config.yaml`](config.yaml). Secrets live in `.env`.

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

## 3. Always-on mode (single container)

One container runs everything: the Telegram bot (instant buttons, `/run`) **and**
the scheduler (every `schedule.interval_hours` ± jitter) via `python -m jobradar serve`,
which is the Dockerfile's default command.

```bash
docker build -t job-radar .
docker run -d --name job-radar --restart unless-stopped \
  --env-file .env -v $(pwd)/data:/app/data job-radar
```

On a PaaS (auto-deploy on git push):
- Service type: **Docker**, Dockerfile in the repo root, branch `main`.
- Set the env vars from `.env.example` (at least `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) in the platform's panel.
- **Attach a persistent disk at `/app/data`.** Without it, your DB and application history are wiped on every deploy.
- If the platform demands an open port, set `PORT=8080`. `GET /` returns a small health JSON.
- The server must be **outside Iran**, since Telegram and LinkedIn are blocked there.
- Redeploys don't trigger a scrape storm: the schedule continues from the last run time stored in the DB.

## 4. Telegram commands

| Command    | What it does |
|------------|--------------|
| `/stats`   | counts by status, source health, top filter reasons |
| `/saved`   | your ⭐ jobs |
| `/applied` | what you applied to |
| `/run`     | scrape now (`serve` / `bot` mode only) |

Every job message has **✅ Applied / ⏭ Skip / ⭐ Save**, plus **↩️ Undo** after you tap one.

## 5. Tuning

```bash
python -m jobradar stats     # what got filtered and why, source health
# edit config.yaml (filters / scoring / min_score)
python -m jobradar rescore   # re-apply rules to everything not sent yet
python -m jobradar run
```

Tips:
- **Too much noise?** Raise `scoring.min_score` or add patterns to `filters.title_exclude`.
- **Missing good jobs?** Check `stats` → "top filter reasons". If `title: no include match`
  is huge, widen `filters.title_include`.
- Add a country: copy a block in `targets`. Indeed needs `country_indeed`
  (e.g. `qatar`, `saudi arabia`, `romania`); LinkedIn/Bayt just use `location`.

## 6. AI second opinion (optional)

Jobs that pass the keyword filter (plus borderline ones near `min_score`) get reviewed
by an LLM, which knows your profile from `AI_PROFILE`. It returns:

- **fit 0–10** → final score = keyword score + (fit − 5) × `ai.weight`
- **feasible from Iran?** yes / no / unclear. A "no" vetoes the job (hidden location rules, on-site...).
- **red flags**, e.g. "needs AWS" or "German C1"

The card then shows `🤖 8/10 · fintech .NET match · ✅ Iran-remote yes`.

**Fault tolerant by design.** No key, 401/403 (bad or banned key), 429, 5xx, timeouts,
broken JSON, or even a bug in the AI code: the job keeps its keyword score and is
sent normally. An auth error, or 3 failures in a row, switches the AI off for the rest
of that run, and the run summary tells you why. Any OpenAI-compatible provider works;
see `.env.example`.

## 7. When a source gets blocked

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
- [x] **Phase 3**: optional LLM pass (fit, feasible-from-Iran veto, red flags) with keyword fallback
- [ ] **Phase 4**: weekly stats, quick tailored cover-letter drafts per job

## Disclaimer

For personal job hunting. Scraping may violate some sites' terms of service.
**Never run it logged in with your main LinkedIn account.** JobSpy uses LinkedIn's
public, logged-out pages. Keep volumes low.
