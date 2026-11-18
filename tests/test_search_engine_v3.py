from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from jobradar.collectors import arbeitnow, feeds
from jobradar.collectors.base import CollectResult, SourceHealth
from jobradar.db import DB
from jobradar.models import Job
from jobradar.pipeline import ingest, record_health
from jobradar.filters import Evaluator
from jobradar.report import build_report
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_url_canonicalization_strips_tracking_params():
    a = Job(source="s", site="x", title="Engineer", url="https://example.com/jobs/123?utm_source=ln&utm_medium=social&foo=1#apply", company="Acme")
    b = Job(source="s", site="x", title="Engineer", url="https://example.com/jobs/123/?foo=1", company="Acme")
    assert a.url == "https://example.com/jobs/123?foo=1"
    assert b.url == "https://example.com/jobs/123?foo=1"
    assert a.id == b.id


def test_linkedin_url_canonicalization_keeps_view_path():
    j = Job(source="jobspy", site="linkedin", title="Engineer", url="https://www.linkedin.com/jobs/view/12345/?trackingId=abc&refId=def", company="Acme")
    assert j.url == "https://www.linkedin.com/jobs/view/12345"


def test_db_source_state_roundtrip_and_cooldown(tmp_path):
    db = DB(tmp_path / "t.db")
    assert db.get_source_state("arbeitnow") is None
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    db.save_source_state("arbeitnow", cooldown_until=future, consecutive_failures=2, checkpoint={"last_created_at": 123})
    st = db.get_source_state("arbeitnow")
    assert st["cooldown_until"] == future
    assert st["consecutive_failures"] == 2
    assert st["checkpoint"] == {"last_created_at": 123}


def test_db_record_source_run_extended_fields(tmp_path):
    db = DB(tmp_path / "t.db")
    db.record_source_run("arbeitnow", 1, 10, 0, None, skipped=0, duration_sec=1.23, raw_found=50, unique_found=10, details={"x": 1})
    row = db.last_source_runs("arbeitnow", 1)[0]
    assert row["raw_found"] == 50
    assert row["unique_found"] == 10
    assert row["details"]


def test_pipeline_record_health_persists_extended_metrics(tmp_path):
    db = DB(tmp_path / "t.db")
    res = CollectResult()
    res.call("arbeitnow", found=5, raw_found=20, unique_found=5, duration_sec=0.5, details={"ok": True})
    res.skip("linkedin", "cooldown", cooldown_until="2099-01-01T00:00:00+00:00")
    record_health(db, res)
    a = db.last_source_runs("arbeitnow", 1)[0]
    assert a["raw_found"] == 20 and a["unique_found"] == 5
    l = db.last_source_runs("linkedin", 1)[0]
    assert l["skipped"] == 1
    assert l["cooldown_until"] == "2099-01-01T00:00:00+00:00"


def test_arbeitnow_skips_when_min_interval_not_elapsed(tmp_path):
    db = DB(tmp_path / "t.db")
    db.save_source_state("arbeitnow", last_success_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    cfg = {"extra_sources": {"arbeitnow": {"enabled": True, "min_fetch_interval_hours": 24}}}
    res = arbeitnow.collect(cfg, session=object(), db=db)
    assert res.jobs == []
    assert res.health["arbeitnow"].skipped == 1
    assert res.health["arbeitnow"].calls == 0


def test_arbeitnow_early_stops_on_old_items(tmp_path):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    old_ts = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp())

    class S:
        def __init__(self):
            self.calls = 0
        def get(self, url, timeout=None, headers=None):
            self.calls += 1
            self.url = url
            return R()

    class R:
        def raise_for_status(self): pass
        def json(self):
            return {
                "data": [{"slug": f"j{i}", "url": f"https://a/{i}", "title": "Backend", "company_name": "C", "remote": True, "created_at": old_ts} for i in range(100)],
                "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
            }

    cfg = {"scrape": {"hours_old": 72}, "extra_sources": {"arbeitnow": {"enabled": True, "pages": 3}}}
    res = arbeitnow.collect(cfg, session=S(), db=DB(tmp_path / "t.db"))
    assert res.health["arbeitnow"].calls == 1  # stopped after first page, all too old
    assert res.health["arbeitnow"].found == 0
    assert res.health["arbeitnow"].raw_found == 100


def test_report_includes_collection_metrics(tmp_path):
    db = DB(tmp_path / "r.db")
    r = CollectResult()
    r.jobs = [
        Job(source="j", site="arbeitnow", title="Senior .NET Developer", url="https://k/1", company="Acme",
            description="C# backend", is_remote=True, target="Arbeitnow", date_posted=datetime.now(timezone.utc).date()),
    ]
    ingest(db, Evaluator(CFG), r)
    run_id = db.start_run("schedule")
    db.finish_run(run_id, {"new": 1, "duplicate": 0, "filtered": 0, "low": 0, "queued": 1, "sent": 0, "raw_found": 5},
                  {"arbeitnow": {"calls": 1, "found": 1, "errors": 0, "last_error": None, "raw_found": 5, "unique_found": 1}}, [])
    rep = build_report(db, CFG, "24h")
    assert rep["funnel"]["raw_found"] >= 5
    assert "collection" in rep
    assert rep["collection"]["raw_found"] >= 5


class _FeedResp:
    def __init__(self, text="", status=200, headers=None, json_data=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        self._json = json_data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)
    def json(self):
        return self._json


class _FeedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_remoteok_conditional_fetch_uses_304(tmp_path):
    db = DB(tmp_path / "t.db")
    db.save_source_state("remoteok", etag="etag1", last_success_at=(datetime.now(timezone.utc)-timedelta(hours=5)).isoformat(timespec="seconds"))
    sess = _FeedSession([_FeedResp(status=304, headers={"ETag": "etag1"})])
    cfg = {"extra_sources": {"feeds": {"enabled": True, "hn": False, "remoteok": True, "wwr_rss": []}}}
    res = feeds.collect(cfg, session=sess, db=db)
    assert res.health["remoteok"].calls == 1
    assert res.health["remoteok"].found == 0
    assert res.health["remoteok"].details["not_modified"] is True
    assert sess.calls[0][1]["headers"].get("If-None-Match") == "etag1"
