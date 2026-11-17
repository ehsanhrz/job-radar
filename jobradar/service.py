"""Coordinated single-process runtime for polling and scheduled job runs."""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

LAST_RUN_KEY = "last_run_at"


def seconds_until_next_run(database: DB, interval_seconds: float, jitter_seconds: float,
                           jitter: Callable[[float, float], float] = random.uniform) -> float:
    """Return the persisted schedule delay, or zero for a brand-new database.

    ``run_once`` records ``last_run_at`` before collector work starts.  Scheduling
    from that durable value prevents a deploy or service restart from immediately
    repeating a run that just started or completed.  Jitter remains part of the
    regular cadence but never shortens a restart delay below five minutes.
    """
    last = database.get_meta(LAST_RUN_KEY)
    if not last:
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


class RunCoordinator:
    """Allows at most one run_once execution; manual requests never queue."""

    def __init__(self, run: Callable[[], object]):
        self._run = run
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    def execute_scheduled(self) -> bool:
        if not self._lock.acquire(blocking=False):
            log.info("scheduled execution skipped: another run is active")
            return False
        try:
            self._run()
            return True
        except Exception:  # noqa: BLE001
            log.exception("scheduled run failed")
            return False
        finally:
            self._lock.release()

    def start_manual(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False

        def worker() -> None:
            try:
                self._run()
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
    execute = run or (lambda: run_once(settings, process_updates=False))
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
    try:
        while not stop.is_set():
            delay = seconds_until_next_run(database, interval, jitter_seconds, jitter)
            if delay > 0:
                log.info("next scheduled run in %.0f min", delay / 60)
                if wait_fn(delay):
                    break
            if stop.is_set():
                break
            coordinator.execute_scheduled()
    finally:
        stop.set()
        bot.join()
        coordinator.wait()
        database.conn.close()
        log.info("service stopped cleanly")
