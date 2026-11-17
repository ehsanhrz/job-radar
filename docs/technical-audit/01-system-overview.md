# 01 — System overview

## Repository and runtime

The application is Python 3.10+ according to README, packaged as a small source tree rather than a multi-project solution. Runtime dependencies are `python-jobspy==1.4.1`, `requests[socks]`, PyYAML, and python-dotenv (`requirements.txt`). Tests use pytest (`requirements-dev.txt`). The Docker image is Python 3.12 slim (`Dockerfile:1`), runs `python -m jobradar`, copies only `jobradar/` and `config.yaml`, and creates `/app/data` (`Dockerfile:7-18`).

| Area | Responsibility | Evidence |
|---|---|---|
| CLI and process startup | Commands `run`, `loop`, `bot`, `stats`, `rescore`, Telegram setup commands | `jobradar/__main__.py:22-137` |
| Settings | YAML config, `.env`, proxies, DB path | `jobradar/config.py:13-45` |
| Domain-shaped record | `Job`, normalization, URL ID, duplicate fingerprint | `jobradar/models.py:9-57` |
| Collectors | JobSpy board calls, Arbeitnow API, aggregate result/health | `jobradar/collectors/` |
| Matching | Regex filters, weighted score, queue/low/filtered result | `jobradar/filters.py:30-105` |
| Orchestration | Collection, health persistence, ingest, delivery, watchdog | `jobradar/pipeline.py:15-120` |
| Persistence | SQLite schema and direct SQL operations | `jobradar/db.py:10-170` |
| User interaction | Telegram HTTP wrapper, message rendering, callbacks, commands | `jobradar/telegram.py`, `jobradar/bot.py` |
| Tests | Mocked collector, pipeline, DB, Telegram client behavior | `tests/test_pipeline.py:58-263` |

There is no `.github/` directory or checked-in CI workflow. No AI package/provider SDK appears in `requirements.txt`; no AI provider or prompt code appears in `jobradar/`.

## Entry points and runtime modes

`main()` in `jobradar/__main__.py:111-137` parses arguments, initializes logging, loads settings, and dispatches a command. `run` calls `pipeline.run_once`; `loop` calls it indefinitely after a configured delay with jitter; `bot` starts Telegram long polling. Docker Compose runs two services over the same `./data` directory: one scheduled worker and one bot (`docker-compose.yml:3-20`). The bot may start a run in a daemon thread after `/run` (`jobradar/bot.py:86-103`). Its lock prevents duplicate manual runs only inside that bot process; it does not coordinate with the worker process.

## Dependency and boundary assessment

The dependency direction is simple: `pipeline` imports collectors, config, DB, evaluator, and Telegram; collectors construct `Job`; DB persists `Job`; bot invokes pipeline and DB. This small graph is easy to navigate. The `filters.Evaluator` is the main business decision point, but its inputs are a broad `Job` record and a raw config dictionary, and its output (`Verdict`) mixes filtering and ranking without a richer domain decision model.

Graphify on the current checkout produced 191 nodes and 469 edges across 10 communities. Its call graph has `run_once()` calling `collect_all`, `record_health`, `Evaluator`, `ingest`, `send_queued`, and `watchdog`; it shows Telegram and bot in a single connected community. This matches source inspection: the architecture is a direct call chain, not a message-broker or service architecture. Graphify did not model deployment/YAML/ZIP artifacts, and its package version warning means graph output should be treated as navigational evidence.

## Config and deployment

`load_settings` reads YAML and environment variables, creates the DB parent directory, and defaults to `data/jobradar.db` (`jobradar/config.py:25-45`). Secrets are expected in `.env` and `.env` is ignored by `.gitignore`; Compose injects that file into both containers. `.env.example` documents Telegram credentials, proxies, and paths without real credentials. Docker uses an unpinned base image tag and pip installs runtime requirements without a lockfile. Compose has restart policies but no health checks, resource limits, or explicit graceful-shutdown configuration.

## Sources actually enabled

The active registry in `jobradar/collectors/__init__.py:7-10` calls exactly `jobspy_collector.collect` and `arbeitnow.collect`. JobSpy is configured for LinkedIn, Indeed, Bayt, and target locations in `config.yaml:26-66`; Glassdoor is supported by generic collector logic but is not configured. Arbeitnow is enabled with `pages: 3` (`config.yaml:68-70`). `script.py` fetches a Jaabz HTML page for offline inspection but is not imported by runtime code. The remaining source names in the task prompt have no current implementation in this checkout.
