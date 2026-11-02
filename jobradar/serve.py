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
FINISHED_KEY = "last_run_finished_at"
NEXT_RUN_KEY = "next_run_at"  # one-off override (resume after a killed run)
HANDLED_KEY = "interrupted_run_handled"  # start time of the killed run already rescheduled
RESUME_AFTER_SEC = 300


def run_was_interrupted(db: DB) -> bool:
    """A run started but never finished (container restarted/redeployed mid-run)."""
    started, finished = db.get_meta(LAST_RUN_KEY), db.get_meta(FINISHED_KEY)
    if not started or db.get_meta(HANDLED_KEY) == started:
        return False
    return not finished or finished < started


def reschedule_interrupted(db: DB, interval_s: float) -> bool:
    """Call once at boot: if the previous run was killed, run again in ~5 min
    instead of waiting a full interval. Real timestamps are left untouched."""
    if not run_was_interrupted(db):
        return False
    from datetime import timedelta
    resume = datetime.now(timezone.utc) + timedelta(seconds=RESUME_AFTER_SEC)
    db.set_meta(NEXT_RUN_KEY, resume.isoformat(timespec="seconds"))
    # mark the killed run as handled so it isn't rescheduled again on the next boot.
    # Not via FINISHED_KEY: that drives the /run cooldown and /stats "last run".
    db.set_meta(HANDLED_KEY, db.get_meta(LAST_RUN_KEY))
    return True


def _seconds_until_next(db: DB, interval_s: float, jitter_s: float) -> float:
    override = db.get_meta(NEXT_RUN_KEY)
    if override:
        return max(60.0, (datetime.fromisoformat(override) - datetime.now(timezone.utc)).total_seconds())
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
    if reschedule_interrupted(db, interval_s):
        log.info("previous run was interrupted (restart/redeploy): running again in ~5 min")
    jitter = random.uniform(-jitter_s, jitter_s)
    announced = None
    while True:
        # re-evaluated every minute: a manual /run moves the next slot back
        # jitter spreads the regular cadence; a one-off resume slot runs on time
        wait = _seconds_until_next(db, interval_s, 0) + (0 if db.get_meta(NEXT_RUN_KEY) else jitter)
        if wait > 60:
            if announced is None or abs(announced - wait) > 600:
                log.info("next scheduled run in %.0f min", wait / 60)
            announced = wait
            time.sleep(60)
            continue
        if try_start_run(settings, notify_busy=False, background=False, trigger="schedule"):
            jitter = random.uniform(-jitter_s, jitter_s)
            announced = None
        else:
            time.sleep(300)  # a /run is in progress


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


def _announce_start(settings: Settings) -> None:
    """Tells you on every (re)deploy whether the DB survived."""
    from .telegram import Telegram, TelegramError
    db = DB(settings.db_path)
    st = db.stats()
    total = sum(st.values())
    created = (db.get_meta("db_created_at") or "")[:16].replace("T", " ")
    last = (db.get_meta(LAST_RUN_KEY) or "never")[:16].replace("T", " ")
    warn = ("\n⚠️ <b>Empty database.</b> If you saw jobs before, your disk is NOT persistent: "
            "mount a persistent volume at /app/data.") if total == 0 and created else ""
    try:
        Telegram(settings.telegram_token, settings.telegram_chat_id, settings.telegram_proxy).send(
            f"🟢 <b>Job Radar started</b>\nDB since {created} UTC · {total} jobs · "
            f"applied {st.get('applied', 0)} · last run {last}{warn}")
    except TelegramError as err:
        log.warning("start message failed: %s", err)


def serve(settings: Settings) -> None:
    _announce_start(settings)
    threading.Thread(target=_scheduler, args=(settings,), daemon=True, name="scheduler").start()
    port = os.getenv("PORT")
    if port:
        threading.Thread(target=_health_server, args=(settings, int(port)), daemon=True,
                         name="health").start()
        log.info("health endpoint on :%s", port)
    run_bot(settings)  # blocks forever
