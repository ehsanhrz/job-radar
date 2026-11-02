"""ATS auto-discovery + watcher: payloads shaped like the public Greenhouse / Lever / Ashby / Workable APIs."""
from __future__ import annotations

from pathlib import Path

import pytest
import requests
import yaml

from jobradar.collectors import ats
from jobradar.collectors.base import CollectResult
from jobradar.db import DB
from jobradar.filters import Evaluator
from jobradar.models import Job

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
EV = Evaluator(CFG)

GH = {"jobs": [
    {"id": 1, "title": "Senior Backend Engineer (.NET)", "absolute_url": "https://boards.greenhouse.io/acmepay/jobs/1",
     "location": {"name": "Remote - EMEA"}, "updated_at": "2026-10-30T10:00:00Z",
     "content": "&lt;p&gt;C# and &lt;b&gt;PostgreSQL&lt;/b&gt;, payments&lt;/p&gt;", "company_name": "Acme Pay"},
    {"id": 2, "title": "Office Manager", "absolute_url": "https://boards.greenhouse.io/acmepay/jobs/2",
     "location": {"name": "Remote"}, "content": ""},
    {"id": 3, "title": "Senior Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acmepay/jobs/3",
     "location": {"name": "Berlin, Germany"}, "content": "Kotlin"},
]}
LEVER = [
    {"id": "a", "text": "Backend Engineer (C#)", "hostedUrl": "https://jobs.lever.co/globex/a",
     "workplaceType": "remote", "categories": {"location": "Anywhere"}, "descriptionPlain": ".NET 8 microservices",
     "lists": [{"text": "Requirements", "content": "<li>Kafka</li>"}], "createdAt": 1793534400000},
    {"id": "b", "text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/globex/b",
     "workplaceType": "hybrid", "categories": {"location": "Lisbon"}},
]
ASHBY = {"jobs": [
    {"title": "Senior Software Engineer, Backend", "jobUrl": "https://jobs.ashbyhq.com/Hooli/1",
     "location": "Remote", "isRemote": True, "descriptionPlain": "Node.js, TypeScript, Postgres",
     "publishedAt": "2026-10-31T09:00:00Z"},
    {"title": "Backend Engineer", "jobUrl": "https://jobs.ashbyhq.com/Hooli/2", "location": "NYC",
     "workplaceType": "OnSite"},
    {"title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/Hooli/3", "isListed": False},
]}
WORKABLE = {"name": "Initech", "jobs": [
    {"title": ".NET Developer", "url": "https://apply.workable.com/initech/j/ABC/", "telecommuting": True,
     "city": "", "country": "Türkiye", "description": "<p>C#, SQL Server</p>", "published_on": "2026-10-29"},
    {"title": "Backend Developer", "url": "https://apply.workable.com/initech/j/DEF/", "telecommuting": False,
     "city": "Istanbul", "country": "Türkiye"},
]}
US_BOARD = {"jobs": [{"title": f"Backend Engineer {i}", "absolute_url": f"https://boards.greenhouse.io/usco/jobs/{i}",
                      "location": {"name": loc}} for i, loc in
                     enumerate(["Remote - US", "New York, NY", "San Francisco, CA", "Austin, TX", "United States"])]}


# ---------------- discovery ----------------
@pytest.mark.parametrize("text,expected", [
    ("Apply: https://boards.greenhouse.io/acmepay/jobs/123", {("greenhouse", "acmepay")}),
    ("https://job-boards.greenhouse.io/AcmePay", {("greenhouse", "acmepay")}),
    ("see jobs.lever.co/globex/abc-123 or https://jobs.ashbyhq.com/Hooli",
     {("lever", "globex"), ("ashby", "Hooli")}),
    ("https://apply.workable.com/initech/j/ABC123/", {("workable", "initech")}),
    ("https://apply.workable.com/j/ABC123", set()),                        # shortlink, no company
    ("https://boards.greenhouse.io/embed/job_board?for=acmepay", {("greenhouse", "acmepay")}),
    ("https://job-boards.eu.greenhouse.io/acme", set()),                   # EU host has no public API
    ("nothing to see at https://example.com/careers", set()),
])
def test_find_boards(text, expected):
    assert ats.find_boards(text) == expected


def test_discover_and_backfill_once(tmp_path):
    db = DB(tmp_path / "a.db")
    jobs = [Job(source="x", site="hn", title="t", url="https://news.ycombinator.com/item?id=1",
                company="Acme Pay", description="Apply https://boards.greenhouse.io/acmepay/jobs/9"),
            Job(source="x", site="remoteok", title="t", url="https://jobs.lever.co/globex/1", company="Globex")]
    assert ats.discover(db, jobs) == 2
    assert ats.discover(db, jobs) == 0                                      # no duplicates
    db.conn.execute("INSERT INTO jobs (id, fingerprint, source, site, title, url, description, status, first_seen)"
                    " VALUES ('z','z','x','x','t','https://jobs.ashbyhq.com/Hooli/1',NULL,'sent','now')")
    db.conn.commit()
    assert ats.backfill_once(db) == 1 and ats.backfill_once(db) == 0       # only the first time


# ---------------- parsers ----------------
def test_parse_greenhouse():
    jobs = ats.parse_greenhouse(GH, "acmepay", None)
    assert len(jobs) == 3
    j = jobs[0]
    assert j.company == "Acme Pay" and j.is_remote and j.location == "Remote: Remote - EMEA"
    assert j.description == "C# and PostgreSQL , payments" or "PostgreSQL" in j.description
    assert jobs[2].is_remote is False                                        # Berlin, no remote word


def test_parse_lever():
    jobs = ats.parse_lever(LEVER, "globex", "Globex")
    assert [j.is_remote for j in jobs] == [True, False]
    assert "Kafka" in jobs[0].description and jobs[0].location == "Remote: Anywhere"


def test_parse_ashby():
    jobs = ats.parse_ashby(ASHBY, "Hooli", "Hooli")
    assert [j.title for j in jobs] == ["Senior Software Engineer, Backend", "Backend Engineer"]  # unlisted dropped
    assert jobs[0].is_remote and jobs[1].is_remote is False


def test_parse_workable():
    jobs = ats.parse_workable(WORKABLE, "initech", None)
    assert jobs[0].company == "Initech" and jobs[0].is_remote and "SQL Server" in jobs[0].description
    assert jobs[1].is_remote is False


def test_parsers_survive_garbage():
    assert ats.parse_greenhouse({}, "x", None) == [] and ats.parse_lever([{"bad": 1}, "x"], "x", None) == []
    assert ats.parse_ashby({"jobs": [{}]}, "x", None) == [] and ats.parse_workable({"jobs": None}, "x", None) == []


def test_us_detection():
    assert ats.looks_us_based(ats.parse_greenhouse(US_BOARD, "usco", None))
    assert not ats.looks_us_based(ats.parse_greenhouse(GH, "acmepay", None))
    assert not ats.looks_us_based(ats.parse_greenhouse(US_BOARD, "usco", None)[:2])   # too few to judge


# ---------------- watcher ----------------
class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp() if callable(resp) else resp
        return Resp(404)


def _seed(db, *boards):
    for a, s in boards:
        db.conn.execute("INSERT INTO ats_companies (ats, slug, discovered_at) VALUES (?, ?, ?)", (a, s, "2026-11-01"))
    db.conn.commit()


def test_watch_keeps_only_remote_relevant(tmp_path):
    db = DB(tmp_path / "w.db")
    _seed(db, ("greenhouse", "acmepay"), ("lever", "globex"), ("ashby", "Hooli"), ("workable", "initech"))
    sess = Session({"greenhouse.io/v1/boards/acmepay": Resp(200, GH), "lever.co/v0/postings/globex": Resp(200, LEVER),
                    "ashbyhq.com/posting-api/job-board/Hooli": Resp(200, ASHBY),
                    "workable.com/api/v1/widget/accounts/initech": Resp(200, WORKABLE)})
    res = ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    titles = sorted(j.title for j in res.jobs)
    assert titles == [".NET Developer", "Backend Engineer (C#)", "Senior Backend Engineer (.NET)",
                      "Senior Software Engineer, Backend"]          # no office manager, no on-site/hybrid
    row = db.conn.execute("SELECT * FROM ats_companies WHERE slug='acmepay'").fetchone()
    assert row["last_found"] == 3 and row["last_kept"] == 1 and row["company"] == "Acme Pay"


def test_watch_disables_after_failures_and_skips_us(tmp_path):
    db = DB(tmp_path / "f.db")
    _seed(db, ("greenhouse", "gone"), ("greenhouse", "usco"))
    sess = Session({"boards/usco": Resp(200, US_BOARD)})
    for _ in range(3):
        db.conn.execute("UPDATE ats_companies SET last_checked=NULL")
        ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    rows = {r["slug"]: r for r in db.conn.execute("SELECT * FROM ats_companies")}
    assert rows["gone"]["disabled"].startswith("3 failures")
    assert rows["usco"]["disabled"] == "looks US-based (sanctions)"
    n = len(sess.calls)
    ats.watch(db, CFG, session=sess, sleep_fn=lambda s: None)
    assert len(sess.calls) == n                                    # disabled boards aren't fetched again


def test_watch_stops_on_429_and_rotates(tmp_path):
    db = DB(tmp_path / "r.db")
    _seed(db, *[("greenhouse", f"co{i}") for i in range(5)])
    sess = Session({"boards/co1": Resp(429)} | {f"boards/co{i}": Resp(200, {"jobs": []}) for i in (0, 2, 3, 4)})
    res = ats.watch(db, CFG, session=sess, sleep_fn=lambda s: None)
    assert len(sess.calls) == 2 and res.health["greenhouse"].errors == 1     # co0 ok, co1 429 -> stop
    cfg = {**CFG, "ats": {**CFG["ats"], "max_companies_per_run": 2}}
    sess2 = Session({"greenhouse.io": Resp(200, {"jobs": []})})
    ats.watch(db, cfg, session=sess2, sleep_fn=lambda s: None)
    assert all("co0" not in c for c in sess2.calls)                          # never-checked go first


def test_run_step_never_raises(tmp_path, monkeypatch):
    db = DB(tmp_path / "x.db")
    monkeypatch.setattr(ats, "watch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ats.run_step(db, CFG, CollectResult(), keep=None).jobs == []


def test_disabled_in_config_does_nothing(tmp_path):
    db = DB(tmp_path / "d.db")
    res = ats.run_step(db, {**CFG, "ats": {"enabled": False}}, CollectResult(), keep=None)
    assert res.jobs == [] and db.conn.execute("SELECT COUNT(*) c FROM ats_companies").fetchone()["c"] == 0


# ---------------- end to end through the real pipeline ----------------
def test_pipeline_discovers_watches_and_sends(tmp_path, monkeypatch):
    from jobradar import pipeline
    from jobradar.config import Settings

    found = CollectResult()
    found.jobs = [Job(source="feeds", site="hn", title="Acme Pay | Backend | REMOTE", is_remote=True,
                      url="https://news.ycombinator.com/item?id=7", company="Acme Pay", location="Remote: Worldwide",
                      description="Apply at https://boards.greenhouse.io/acmepay/jobs/1")]
    found.call("hn", found=1)
    monkeypatch.setattr(pipeline, "collect_all", lambda cfg, proxies, want_details=None: found)
    monkeypatch.setattr(ats.requests, "Session", lambda: Session({"boards/acmepay": Resp(200, GH)}))
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)

    class TG:
        def __init__(self, *a, **k):
            self.sent = []

        def send(self, text, buttons=None):
            self.sent.append(text)
            return 1
    tg = TG()
    monkeypatch.setattr(pipeline, "Telegram", lambda *a, **k: tg)
    s = Settings(raw={**CFG, "telegram": {"send_run_summary": False, "max_per_run": 20}}, telegram_token="t",
                 telegram_chat_id="1", telegram_proxy=None, scrape_proxies=[], db_path=tmp_path / "p.db")
    summary = pipeline.run_once(s, process_updates=False)
    assert any("Senior Backend Engineer (.NET)" in t for t in tg.sent)       # found via the company board
    assert "greenhouse" in summary["sources"]


def test_companies_command_and_report(tmp_path):
    from jobradar.bot import _companies_text
    from jobradar.report import build_report
    db = DB(tmp_path / "c.db")
    assert "No company boards" in _companies_text(db)
    _seed(db, ("lever", "globex"))
    assert "globex" in _companies_text(db) and "not checked yet" in _companies_text(db)
    rep = build_report(db, CFG, "24h")
    assert rep["ats_companies"]["watched"] == 1


def test_upgrade_on_existing_database(tmp_path):
    """A DB from before this PR (no ats_companies table) upgrades in place and backfills."""
    import sqlite3
    path = tmp_path / "old.db"
    db = DB(path)
    db.conn.execute("DROP TABLE ats_companies")
    db.conn.execute("INSERT INTO jobs (id, fingerprint, source, site, title, url, description, status, first_seen)"
                    " VALUES ('o','o','x','remoteok','t','https://remoteok.com/1',"
                    "'Apply: https://jobs.lever.co/oldco/123','sent','now')")
    db.conn.commit()
    db.conn.close()
    db2 = DB(path)                                  # schema re-applied
    assert ats.run_step(db2, CFG, CollectResult(), keep=None,
                        session=Session({}), sleep_fn=lambda s: None).jobs == []
    row = db2.conn.execute("SELECT * FROM ats_companies").fetchone()
    assert (row["ats"], row["slug"]) == ("lever", "oldco") and row["failures"] == 1   # 404 counted, not fatal


# ---------------- PR #13: daily re-check, dead boards, North America ----------------
def test_company_is_rechecked_at_most_once_per_interval(tmp_path):
    db = DB(tmp_path / "i.db")
    _seed(db, ("greenhouse", "acmepay"))
    sess = Session({"boards/acmepay": Resp(200, GH)})
    ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    assert len(sess.calls) == 1                                     # second run: not due yet
    db.conn.execute("UPDATE ats_companies SET last_checked='2026-01-01T00:00:00+00:00'")
    ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    assert len(sess.calls) == 2


def test_board_with_nothing_relevant_is_disabled(tmp_path):
    db = DB(tmp_path / "e.db")
    _seed(db, ("greenhouse", "defenseco"), ("greenhouse", "acmepay"))
    junk = {"jobs": [{"title": "Mechanical Engineer", "absolute_url": f"https://x/{i}",
                      "location": {"name": "Remote"}} for i in range(5)]}
    sess = Session({"boards/defenseco": Resp(200, junk), "boards/acmepay": Resp(200, GH)})
    for _ in range(3):
        db.conn.execute("UPDATE ats_companies SET last_checked=NULL")
        ats.watch(db, CFG, keep=EV.title_ok, session=sess, sleep_fn=lambda s: None)
    rows = {r["slug"]: r for r in db.conn.execute("SELECT * FROM ats_companies")}
    assert rows["defenseco"]["disabled"] == "nothing relevant in 3 checks"
    assert rows["acmepay"]["disabled"] is None and rows["acmepay"]["empty_checks"] == 0


def test_north_american_board_counts_as_us():
    board = {"jobs": [{"title": f"Engineer {i}", "absolute_url": f"https://x/{i}", "location": {"name": loc}}
                      for i, loc in enumerate(["Remote - US", "Toronto, Canada", "Remote (North America)",
                                               "London, UK", "Remote - Americas"])]}
    assert ats.looks_us_based(ats.parse_greenhouse(board, "naco", None))     # 4/5 >= 0.6


def test_old_ats_table_gets_empty_checks_column(tmp_path):
    path = tmp_path / "old2.db"
    db = DB(path)
    db.conn.execute("DROP TABLE ats_companies")
    db.conn.execute("""CREATE TABLE ats_companies (ats TEXT NOT NULL, slug TEXT NOT NULL, company TEXT,
        discovered_at TEXT NOT NULL, discovered_via TEXT, last_checked TEXT, last_found INTEGER,
        last_kept INTEGER, failures INTEGER NOT NULL DEFAULT 0, disabled TEXT, PRIMARY KEY (ats, slug))""")
    db.conn.execute("INSERT INTO ats_companies (ats, slug, discovered_at) VALUES ('lever','oldco','x')")
    db.conn.commit()
    db.conn.close()
    db2 = DB(path)
    assert db2.conn.execute("SELECT empty_checks FROM ats_companies").fetchone()[0] == 0
