from __future__ import annotations

import threading

from jobradar.bot import APPLIED_PAGE_SIZE, _applied_page, handle_update
from jobradar.config import Settings
from jobradar.db import DB, now
from jobradar.service import RunCoordinator, run_service, seconds_until_next_run


def applied_db(path, count):
    db = DB(path)
    for i in range(count):
        db.conn.execute(
            """INSERT INTO jobs (id,fingerprint,source,site,title,company,location,url,status,first_seen,updated_at,date_posted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"id-{i:03}", f"fp-{i}", "test", "board", f"Title {i}", "Company", "Location",
             f"https://jobs.test/{i}", "applied", "2026-01-01", f"2026-01-{i % 28 + 1:02}", "2026-01-01"),
        )
    db.conn.commit()
    return db


def test_applied_count_pages_and_handler(tmp_path):
    db = applied_db(tmp_path / "applied.db", 91)
    assert db.count_by_status("applied") == 91
    first = db.page_by_status("applied", APPLIED_PAGE_SIZE, 0)
    second = db.page_by_status("applied", APPLIED_PAGE_SIZE, APPLIED_PAGE_SIZE)
    final = db.page_by_status("applied", APPLIED_PAGE_SIZE, 90)
    assert len(first) == 10 and len(second) == 10 and len(final) == 1
    all_pages = [db.page_by_status("applied", APPLIED_PAGE_SIZE, offset)
                 for offset in range(0, 91, APPLIED_PAGE_SIZE)]
    ids = [r["id"] for page in all_pages for r in page]
    assert len(ids) == 91 == len(set(ids))
    tg = type("TG", (), {"send": lambda self, text, buttons=None: sent.append((text, buttons))})()
    sent = []
    settings = Settings({}, "t", "42", None, [], tmp_path / "applied.db")
    handle_update(db, tg, settings, {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/applied"}})
    assert "Applied jobs: 91" in sent[0][0]
    assert "Showing 1–10 of 91" in sent[0][0] and "Page 1 of 10" in sent[0][0]
    assert sent[0][1][0][-1]["callback_data"] == "ap:2"
    sent.clear()
    handle_update(db, tg, settings, {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/applied 10"}})
    assert "Showing 91–91 of 91" in sent[0][0]
    assert "Title " in sent[0][0] and "https://jobs.test/" in sent[0][0]
    assert len(sent[0][0]) < 4096


def test_applied_empty_invalid_and_fields(tmp_path):
    db = applied_db(tmp_path / "empty.db", 0)
    text, buttons = _applied_page(db, 1)
    assert "Applied jobs: 0" in text[0] and "No applied jobs" in text[0] and buttons is None
    assert "Invalid page" in _applied_page(db, 2)[0][0]
    db = applied_db(tmp_path / "fields.db", 1)
    text, _ = _applied_page(db, 1)
    assert "Company" in text[0] and "Location" in text[0] and "https://jobs.test/0" in text[0]
    assert "Posted:" in text[0] and "Updated:" in text[0]


def test_manual_run_is_nonblocking_and_rejects_overlap():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def run():
        entered.set()
        release.wait(2)
        finished.set()

    coord = RunCoordinator(run)
    assert coord.start_manual()
    assert entered.wait(1)
    assert not coord.start_manual()
    assert not coord.execute_scheduled()
    release.set()
    coord.wait(2)
    assert finished.is_set()
    assert coord.execute_scheduled()


def test_scheduler_continues_after_run_exception():
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("expected")

    coord = RunCoordinator(run)
    assert coord.execute_scheduled() is False
    assert coord.execute_scheduled() is True
    assert len(calls) == 2


def test_service_starts_one_poller_runs_a_fresh_db_and_recurs(tmp_path):
    stopped = threading.Event()
    pollers, runs, waits = [], [], []

    def bot(settings, coordinator, stop_event, ready_event):
        pollers.append(threading.current_thread().name)
        ready_event.set()
        stop_event.wait(2)

    def wait(delay):
        waits.append(delay)
        if len(waits) == 1:
            return False
        stopped.set()
        return True

    settings = Settings({"schedule": {"interval_hours": 1, "jitter_minutes": 0}}, "t", "42",
                        None, [], tmp_path / "service.db")
    def run():
        runs.append(1)
        DB(settings.db_path).set_meta("last_run_at", now())

    run_service(settings, stopped, bot_runner=bot, run=run,
                wait=wait, jitter=lambda a, b: 0)
    assert pollers == ["jobradar-telegram-poller"]
    assert len(runs) == 2
    assert len(waits) == 2 and all(3590 <= d <= 3600 for d in waits)
    assert stopped.is_set()


def test_service_restart_uses_persisted_last_run_time(tmp_path):
    path = tmp_path / "restart.db"
    db = DB(path)
    db.set_meta("last_run_at", now())
    assert 3590 <= seconds_until_next_run(db, 3600, 0, lambda a, b: 0) <= 3600

    stopped, waits, runs = threading.Event(), [], []

    def bot(settings, coordinator, stop_event, ready_event):
        ready_event.set()
        stop_event.wait(2)

    def wait(delay):
        waits.append(delay)
        stopped.set()
        return True

    settings = Settings({"schedule": {"interval_hours": 1, "jitter_minutes": 0}}, "t", "42",
                        None, [], path)
    run_service(settings, stopped, bot_runner=bot, run=lambda: runs.append(1),
                wait=wait, jitter=lambda a, b: 0)
    assert runs == []
    assert len(waits) == 1 and 3590 <= waits[0] <= 3600


def test_run_command_uses_coordinator_response(tmp_path):
    db = applied_db(tmp_path / "handler.db", 0)
    sent = []
    tg = type("TG", (), {"send": lambda self, text, buttons=None: sent.append(text)})()
    settings = Settings({}, "t", "42", None, [], tmp_path / "handler.db")

    class Coord:
        def start_manual(self):
            return False

    handle_update(db, tg, settings, {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/run"}}, coordinator=Coord())
    assert sent == ["A run is already in progress."]


def test_run_command_starts_manual_execution(tmp_path):
    db = applied_db(tmp_path / "handler-start.db", 0)
    sent, complete = [], threading.Event()
    tg = type("TG", (), {"send": lambda self, text, buttons=None: sent.append(text)})()
    settings = Settings({}, "t", "42", None, [], tmp_path / "handler-start.db")
    coordinator = RunCoordinator(lambda: complete.set())
    handle_update(db, tg, settings,
                  {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/run"}}, coordinator=coordinator)
    coordinator.wait(1)
    assert complete.is_set() and sent == ["🛰 Starting a run…"]


def test_rescore_shares_the_coordinator_lock(tmp_path):
    from jobradar.bot import handle_update

    db = applied_db(tmp_path / "rescore.db", 0)
    sent = []
    tg = type("TG", (), {"send": lambda self, text, buttons=None: sent.append(text)})()
    settings = Settings({}, "t", "42", None, [], tmp_path / "rescore.db")
    entered, release = threading.Event(), threading.Event()

    def run():
        entered.set()
        release.wait(2)

    coordinator = RunCoordinator(run)
    assert coordinator.start_manual()
    assert entered.wait(1)
    upd = {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/rescore"}}
    handle_update(db, tg, settings, upd, coordinator=coordinator)
    assert "in progress" in sent[-1]
    release.set()
    coordinator.wait(2)
    handle_update(db, tg, settings, upd, coordinator=coordinator)
    assert "Rescored" in sent[-1]


def test_service_resume_override_for_interrupted_run(tmp_path):
    from datetime import datetime, timedelta, timezone

    from jobradar.service import LAST_RUN_KEY, resume_interrupted_if_due

    db = DB(tmp_path / "resume.db")
    db.set_meta(LAST_RUN_KEY, now())   # just started: may still be alive elsewhere
    assert not resume_interrupted_if_due(db, 3600)
    assert 3590 <= seconds_until_next_run(db, 3600, 0, lambda a, b: 0) <= 3600

    killed_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(timespec="seconds")
    db.set_meta(LAST_RUN_KEY, killed_at)
    assert resume_interrupted_if_due(db, 3600)
    delay = seconds_until_next_run(db, 3600, 0, lambda a, b: 0)
    assert 0 < delay <= 310

