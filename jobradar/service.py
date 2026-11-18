"""Coordinated single-process runtime for polling and scheduled job runs."""
from __future__ import annotations

import inspect
import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .db import DB, now

log = logging.getLogger(__name__)

LAST_RUN_KEY = "last_run_at"
NEXT_RUN_KEY = "next_run_at"


def seconds_until_next_run(database: DB, interval_seconds: float, jitter_seconds: float,
                           jitter: Callable[[float, float], float] = random.uniform) -> float:
    """Return the persisted schedule delay, or zero for a brand-new database.

    ``run_once`` records ``last_run_at`` before collector work starts.  Scheduling
    from that durable value prevents a deploy or service restart from immediately
    repeating a run that just started or completed.  Jitter remains part of the
    regular cadence but never shortens a restart delay below five minutes.

    A ``next_run_at`` override is a one-off slot (used to resume a run killed by a
    redeploy) and takes precedence over the regular cadence until it is consumed.
    """
    override = database.get_meta(NEXT_RUN_KEY)
    if override:
        try:
            when = datetime.fromisoformat(override)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = max(60.0, (when - datetime.now(timezone.utc)).total_seconds())
            log.info("resume override active: next run in %.0f min (UTC schedule)", delay / 60)
            return delay
        except (TypeError, ValueError):
            log.warning("invalid %s value %r; using the regular schedule", NEXT_RUN_KEY, override)
    last = database.get_meta(LAST_RUN_KEY)
    if not last:
        log.info("no previous run recorded; scheduling first run immediately")
        return 0.0
    try:
        started = datetime.fromisoformat(last)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        log.warning("invalid %s value %r; scheduling a run now", LAST_RUN_KEY, last)
        return 0.0
    elapsed = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
    remaining = interval_seconds - elapsed + jitter(-jitter_seconds, jitter_seconds)
    return max(300.0, remaining)


def resume_interrupted_if_due(database: DB, interval_seconds: float) -> bool:
    """Schedule a near-term retry for a previous run that never finished.

    The legacy ``serve`` scheduler does this at boot.  A run that started only
    seconds ago may still be alive in another process, so the service path waits
    for the resume grace period before claiming the run was killed.
    """
    from .serve import RESUME_AFTER_SEC, reschedule_interrupted

    last = database.get_meta(LAST_RUN_KEY)
    if not last:
        return False
    try:
        started = datetime.fromisoformat(last)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
    if age < RESUME_AFTER_SEC:
        return False
    return reschedule_interrupted(database, interval_seconds)


class RunCoordinator:
    """Allows at most one run_once execution; manual requests never queue."""

    def __init__(self, run: Callable[..., object]):
        self._run = run
        try:
            self._takes_trigger = len(inspect.signature(run).parameters) >= 1
        except (TypeError, ValueError):
            self._takes_trigger = False
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    def _invoke(self, trigger: str) -> object:
        if self._takes_trigger:
            return self._run(trigger)
        return self._run()

    def execute_scheduled(self, trigger: str = "schedule") -> bool:
        if not self._lock.acquire(blocking=False):
            log.info("scheduled execution skipped: another run is active")
            return False
        try:
            self._invoke(trigger)
            return True
        except Exception:  # noqa: BLE001
            log.exception("scheduled run failed")
            return False
        finally:
            self._lock.release()

    def exclusive(self, action: Callable[[], object]) -> bool:
        """Run a short, non-pipeline action under the same lock as real runs.

        Returns False when a run is already active, so callers can tell the user
        to retry instead of sending duplicate messages.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            action()
            return True
        finally:
            self._lock.release()

    def start_manual(self, trigger: str = "manual") -> bool:
        if not self._lock.acquire(blocking=False):
            return False

        def worker() -> None:
            try:
                self._invoke(trigger)
            except Exception:  # noqa: BLE001
                log.exception("manual run failed")
            finally:
                self._lock.release()
                with self._threads_lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=worker, name="jobradar-manual-run", daemon=False)
        with self._threads_lock:
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._threads_lock:
                self._threads.discard(thread)
            self._lock.release()
            raise
        return True

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._threads_lock:
                threads = list(self._threads)
            if not threads:
                return
            for thread in threads:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return


def run_service(settings: Settings, stop_event: threading.Event | None = None,
                bot_runner: Callable | None = None, run: Callable | None = None,
                wait: Callable[[float], bool] | None = None,
                jitter: Callable[[float, float], float] = random.uniform) -> None:
    """Run one Telegram polling thread and the scheduled pipeline in this process."""
    from .bot import run_bot
    from .pipeline import run_once

    stop = stop_event or threading.Event()
    database = DB(settings.db_path)
    runner = bot_runner or run_bot
    if run is not None:
        try:
            execute = run if len(inspect.signature(run).parameters) >= 1 else (lambda _trigger=None: run())
        except (TypeError, ValueError):
            execute = run
    else:
        execute = lambda trigger="schedule": run_once(settings, process_updates=False, trigger=trigger)
    coordinator = RunCoordinator(execute)
    ready = threading.Event()
    bot = threading.Thread(target=runner, args=(settings, coordinator, stop, ready),
                           name="jobradar-telegram-poller", daemon=False)
    bot.start()
    if not ready.wait(70):
        stop.set()
        bot.join()
        raise RuntimeError("Telegram polling consumer did not start")

    schedule = settings.section("schedule")
    interval = float(schedule.get("interval_hours", 6)) * 3600
    jitter_seconds = float(schedule.get("jitter_minutes", 30)) * 60
    wait_fn = wait or stop.wait
    log.info("service schedule: every %.2f h ± %.0f min (UTC-anchored to last_run_at)",
             interval / 3600, jitter_seconds / 60)
    if resume_interrupted_if_due(database, interval):
        log.info("previous run was interrupted (restart/redeploy): running again soon")
    try:
        while not stop.is_set():
            delay = seconds_until_next_run(database, interval, jitter_seconds, jitter)
            if delay > 0:
                log.info("next scheduled run in %.0f min", delay / 60)
                if wait_fn(delay):
                    break
            if stop.is_set():
                break
            # Record the scheduled attempt in the same database connection that
            # computes the next delay.  run_once records this too for CLI and
            # manual runs; doing it here makes the scheduler's restart contract
            # hold even if setup fails before run_once reaches its own write.
            database.set_meta(LAST_RUN_KEY, now())
            coordinator.execute_scheduled("schedule")
    finally:
        stop.set()
        bot.join()
        coordinator.wait()
        database.conn.close()
        log.info("service stopped cleanly")
