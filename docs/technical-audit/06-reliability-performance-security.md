# 06 — Reliability, performance, security

## Reliability and failure modes

- JobSpy is synchronous and issues calls serially with a configured 4–10 second random delay (`config.yaml:10-14`, `jobspy_collector.py:88-123`). There is no application retry/backoff for 429; the exception becomes a source error. A DataFrame can return jobs while also reporting a per-site error.
- Historical local DB evidence includes LinkedIn's 429 “blocked: use a proxy” on 2 of 17 calls. It still found 260 results in that run. This is a real partial failure; current watchdog tests only the all-zero case (`tests/test_pipeline.py:213-230`).
- JobSpy uses `hours_old=72`; evaluator separately filters dates older than 14 days. When posted date is missing, freshness limit does not reject the job (`config.yaml:10-14, 74`).
- Arbeitnow paginates up to three pages, follows provider-provided `links.next`, uses a 30-second timeout, and stops on first error. No retry or rate-limit backoff exists (`arbeitnow.py:45-75`).
- `run_once` performs collectors, database work, and Telegram sends sequentially. No queue backpressure or parallel evaluation exists. At current local data volume there is no evidence of a performance bottleneck; no benchmark or timing breakdown exists.
- Production `service` runs the poller and scheduler in one process. `RunCoordinator` rejects overlapping manual and scheduled work, and `meta.last_run_at` prevents a normal restart from immediately repeating a run. Separate CLI processes remain outside that in-process lock.
- A successful Telegram send followed by a failed DB write can create duplicate notifications on a later run. A user update exception is logged and the offset advances, potentially losing the action.
- No graceful cancellation/stop handling is visible in the loops; scheduled `loop` sleeps then starts another run, and bot long polling catches Telegram exceptions and sleeps 10s.

## Performance

No runtime measurements were collected. Search calls are the likely latency driver because target × terms × boards is serial and sleeps between calls; current config has 7 targets, 2 base terms (with two extra Turkey terms and one Worldwide term), and several boards. Actual call counts vary because unsupported country searches are skipped. JobSpy returns in-memory DataFrames, and all collected jobs are accumulated before ingestion. At the configured results-per-call 20 and current configuration, memory risk appears limited but was not benchmarked.

## Security review

**Positive evidence:** `.env` is ignored and `.env.example` contains placeholders; settings read credentials from environment (`config.py:25-45`, `.gitignore`). Telegram callback and command handlers require matching configured chat id (`bot.py:50-57, 74-76`). Telegram output escapes title/company/location/reasons and quotes URLs in link attributes (`telegram.py:91-127`). SQL values use placeholders; the only dynamic SQL is a placeholder-count string from the number of statuses (`db.py:115-121`).

**Risks and unverified items:**

- Job descriptions and tags are external, untrusted content. `ai.py` has an optional legacy prompt that explicitly treats the posting as untrusted data. V2 does not call an LLM. If the legacy reviewer is reenabled, prompt-injection tests and a stricter evidence policy are still warranted.
- Arbeitnow `links.next` is consumed as a URL without checking that the host remains `www.arbeitnow.com` (`arbeitnow.py:56-71`). The initial URL is fixed HTTPS. A hostile/compromised API response could cause requests to follow another URL; SSRF is a potential trust-boundary risk, not demonstrated exploitability.
- `script.py` makes an HTTP(S) GET to a hardcoded Jaabz URL with a timeout and fixed User-Agent. It is not part of app runtime and does not validate redirects/response content type.
- The chat-id setup command prints IDs to terminal; Telegram token is embedded in API URL as required by Bot API and is not logged by this code. Logging of full exception text may include provider response details; no explicit redaction layer exists.
- Docker runs as root by default and uses floating `python:3.12-slim`; dependencies are not locked by hashes and no vulnerability scan/CI is present. These are hardening risks, not evidence of a known vulnerable package.
- External scraped job links are shown to the user. They are HTML-escaped, but the app does not validate allowed URL schemes/hosts before presenting links. Source URLs are expected to be HTTPS but this is not enforced.
- The actual `.env` file was not read. Its contents and production secret handling remain unverified.

## Sanctions/compliance boundary

Repository code does not implement sanctions screening or country-specific legal eligibility. No legal conclusion is made. Employer/platform country policies and payment/EOR capabilities are outside the code evidence and must be verified from reliable current sources before labeling a real vacancy eligible or ineligible.
