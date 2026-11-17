# Evidence index

Claims are based on the audited checkout. Line numbers refer to source paths in the repository root and may shift if files change.

| Topic / finding | Evidence |
|---|---|
| CLI entry and command dispatch | `jobradar/__main__.py:22-137` — `cmd_run`, `cmd_loop`, `cmd_rescore`, `main` |
| Settings and secret environment variables | `jobradar/config.py:13-45`; `.env.example:1-20`; `.gitignore` |
| Current collectors | `jobradar/collectors/__init__.py:7-10`; `jobradar/collectors/jobspy_collector.py:75-143`; `jobradar/collectors/arbeitnow.py:45-75` |
| Discovery normalization | `jobradar/models.py:22-57`; `jobspy_collector.py:55-72`; `arbeitnow.py:19-42` |
| Source health | `jobradar/collectors/base.py:9-36`; `jobradar/pipeline.py:29-60` |
| Pipeline ordering | `jobradar/pipeline.py:15-120` — `ingest`, `watchdog`, `send_queued`, `run_once` |
| Hard filters and scoring | `jobradar/filters.py:37-105`; `config.yaml:74-145` |
| No AI evaluator | `requirements.txt`; `jobradar/`; README roadmap `README.md:123-128` |
| Geographic fields and remote flag | `jobradar/models.py:22-36`; `jobspy_collector.py:60-72`; `arbeitnow.py:31-42`; `filters.py:86-90`; `db.py:11-34` |
| DB schema, indexes, dedupe, states | `jobradar/db.py:10-170`; `jobradar/models.py:39-52` |
| Telegram delivery and status callbacks | `jobradar/pipeline.py:63-80`; `jobradar/telegram.py:26-127`; `jobradar/bot.py:50-93` |
| Worker/bot concurrency | `docker-compose.yml:3-20`; `jobradar/bot.py:86-103`; `jobradar/__main__.py:27-42` |
| Test inventory | `tests/test_pipeline.py:58-263`; `tests/conftest.py`; `requirements-dev.txt` |
| Deployment | `Dockerfile:1-18`; `docker-compose.yml:1-20` |
| Scraping tool vs active source | `script.py:1-20` is a Jaabz HTML downloader but is not imported by `jobradar/collectors/__init__.py` |
| Graphify call structure | `graphify-out/graph.json`; query executed on refreshed graph reported 191 nodes, 469 edges, 10 communities; call edges shown in investigation log |
| Stored job/status/source aggregate | Read-only SQL against `data/jobradar.db` on audit date; 1,158 jobs; status/source/source_runs values summarized in README and sections 04-06 |
| Real recorded LinkedIn 429 | Read-only aggregate query of `source_runs.last_error`: 2 errors in a 17-call LinkedIn run; exact message was a 429/proxy block |

## Scope limitations

- `.env` was not opened; real credentials and local secret hygiene remain unverified.
- The SQLite DB was queried read-only and only for aggregate counts and source error metadata. No job description, company, or applicant data was inspected.
- No external services were contacted; current employer policies, sanctions restrictions, provider terms, and live source health remain unknown.
- No performance benchmark, coverage report, CI run, vulnerability scanner, or container image scan was available in the repository/tool workflow.
- Current Graphify graph was refreshed after prior-turn application edits were reverted. It skips some unsupported file types and is used only as a navigation aid.
