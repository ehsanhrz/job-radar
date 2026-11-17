"""Coordinated single-process runtime for polling and scheduled job runs."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


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
    database.conn.close()
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
            coordinator.execute_scheduled()
            if stop.is_set():
                break
            delay = max(300, interval + jitter(-jitter_seconds, jitter_seconds))
            log.info("next scheduled run in %.0f min", delay / 60)
            if wait_fn(delay):
                break
    finally:
        stop.set()
        bot.join()
        coordinator.wait()
        log.info("service stopped cleanly")
