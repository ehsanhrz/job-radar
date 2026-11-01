"""Single-process mode for one-container hosting (no docker compose).

- Telegram bot (buttons, /stats, /run...) on the main thread
- scheduler thread: scrapes every `schedule.interval_hours`
- optional tiny HTTP health endpoint if the platform sets $PORT

Restarts (e.g. a redeploy on every git push) do NOT trigger an immediate scrape:
the last run time is stored in the DB and the schedule continues from there.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bot import run_bot, try_start_run
from .config import Settings
from .db import DB

log = logging.getLogger("serve")

LAST_RUN_KEY = "last_run_at"


def _seconds_until_next(db: DB, interval_s: float, jitter_s: float) -> float:
    last = db.get_meta(LAST_RUN_KEY)
    if not last:
        return 60.0  # fresh install: first run a minute after boot
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    return max(60.0, interval_s - elapsed + random.uniform(-jitter_s, jitter_s))


def _scheduler(settings: Settings) -> None:
    sched = settings.section("schedule")
    interval_s = float(sched.get("interval_hours", 6)) * 3600
    jitter_s = float(sched.get("jitter_minutes", 30)) * 60
    db = DB(settings.db_path)
    while True:
        wait = _seconds_until_next(db, interval_s, jitter_s)
        log.info("next scheduled run in %.0f min", wait / 60)
        time.sleep(wait)
        if not try_start_run(settings, notify_busy=False, background=False):
            log.info("a run is already in progress, skipping this slot")
            time.sleep(300)


def _health_server(settings: Settings, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            db = DB(settings.db_path)
            body = json.dumps({
                "ok": True,
                "last_run_at": db.get_meta(LAST_RUN_KEY),
                "jobs": db.stats(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep logs quiet
            pass

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def serve(settings: Settings) -> None:
    threading.Thread(target=_scheduler, args=(settings,), daemon=True, name="scheduler").start()
    port = os.getenv("PORT")
    if port:
        threading.Thread(target=_health_server, args=(settings, int(port)), daemon=True,
                         name="health").start()
        log.info("health endpoint on :%s", port)
    run_bot(settings)  # blocks forever
