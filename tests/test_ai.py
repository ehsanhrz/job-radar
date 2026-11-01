"""The AI layer must never break a run: every failure falls back to keyword scores."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests
import yaml

from jobradar.ai import AIReviewer, Review, apply_review, format_review, parse_review
from jobradar.collectors.base import CollectResult
from jobradar.db import DB
from jobradar.filters import Evaluator
from jobradar.models import Job
from jobradar.pipeline import ai_review_step, ingest, send_queued

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
ENV = {"AI_BASE_URL": "https://ai.test/v1", "AI_API_KEY": "k", "AI_MODEL": "m", "AI_PROFILE": "me"}


class Resp:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._payload, self.text = payload, text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def ok(content: str) -> Resp:
    return Resp(200, {"choices": [{"message": {"content": content}}]})


GOOD = json.dumps({"fit": 8, "feasible": "yes", "reason": "fintech .NET match", "flags": ["AWS"]})


class Session:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, *responses):
        self.responses, self.calls, self.bodies = list(responses), 0, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        self.bodies.append(json)
        r = self.responses.pop(0) if self.responses else ok(GOOD)
        if isinstance(r, Exception):
            raise r
        return r


def reviewer(*responses, env=ENV, cfg=CFG):
    return AIReviewer(cfg, env=env, session=Session(*responses), sleep_fn=lambda s: None)


JOB = {"title": "Senior .NET Engineer", "company": "Acme", "location": "Dubai", "is_remote": 1,
       "description": "C#, payments"}


# ---------- parsing ----------
@pytest.mark.parametrize("text", [
    GOOD,
    "```json\n" + GOOD + "\n```",
    "Sure! Here you go: " + GOOD + " hope it helps",
])
def test_parse_tolerates_wrapping(text):
    r = parse_review(text)
    assert r and r.fit == 8 and r.feasible == "yes"


@pytest.mark.parametrize("text", ["", "no json here", '{"fit": "high"}', '{"fit": 42}', "{broken"])
def test_parse_rejects_garbage(text):
    assert parse_review(text) is None


def test_parse_normalises_odd_values():
    r = parse_review('{"fit": 7.6, "feasible": "MAYBE", "reason": "x", "flags": "AWS"}')
    assert r.fit == 8 and r.feasible == "unclear" and r.flags == ["AWS"]


# ---------- failure modes -> None (fallback) ----------
@pytest.mark.parametrize("bad", [
    Resp(401), Resp(403), Resp(404), Resp(429, headers={"retry-after": "1"}), Resp(500), Resp(503),
    Resp(200, payload=None),                       # 200 but not JSON
    Resp(200, payload={"error": "weird"}),         # 200 but wrong shape
    ok("I cannot help with that."),                # model refused / no JSON
    requests.Timeout("slow"), requests.ConnectionError("dns"), RuntimeError("anything"),
])
def test_every_failure_returns_none(bad):
    assert reviewer(bad).review(JOB) is None


def test_no_key_means_disabled_and_no_calls():
    r = AIReviewer(CFG, env={}, session=Session())
    assert not r.enabled and r.why_disabled() == "not configured"
    assert r.review(JOB) is None and r.s.calls == 0


def test_partial_config_reports_what_is_missing():
    r = AIReviewer(CFG, env={"AI_API_KEY": "k"})
    assert r.why_disabled() == "missing AI_BASE_URL, AI_MODEL"


def test_auth_error_trips_immediately():
    r = reviewer(Resp(403))
    assert r.review(JOB) is None and r.tripped
    assert r.why_disabled() == "key rejected (HTTP 403)"
    assert r.review(JOB) is None and r.s.calls == 1  # no more calls this run


def test_circuit_breaker_after_consecutive_failures():
    r = reviewer(Resp(500), Resp(500), Resp(500))
    for _ in range(3):
        r.review(JOB)
    assert r.tripped and r.s.calls == 3
    r.review(JOB)
    assert r.s.calls == 3


def test_success_resets_failure_counter():
    r = reviewer(Resp(500), Resp(500), ok(GOOD), Resp(500), Resp(500))
    for _ in range(5):
        r.review(JOB)
    assert not r.tripped


# ---------- scoring ----------
def test_apply_review():
    assert apply_review(40, None, CFG) == (40, None)
    assert apply_review(40, Review(8, "yes", "ok"), CFG) == (55, None)
    assert apply_review(40, Review(3, "yes", "meh"), CFG) == (30, None)
    _, veto = apply_review(40, Review(9, "no", "EU residence required"), CFG)
    assert veto and "not feasible" in veto
    _, veto = apply_review(80, Review(1, "yes", "Java role"), CFG)
    assert veto and "poor fit" in veto


def test_format_review_escapes_model_output():
    out = format_review(Review(7, "unclear", "<b>hack</b> & co", ["needs <AWS>"]).to_json())
    assert "&lt;b&gt;hack" in out and "&lt;AWS&gt;" in out and "7/10" in out
    assert format_review(None) is None and format_review("garbage") is None


# ---------- pipeline integration ----------
def _db_with_jobs(tmp_path):
    db = DB(tmp_path / "t.db")
    r = CollectResult()
    r.jobs = [
        Job(source="j", site="linkedin", title="Senior .NET Developer", url="https://x/1",
            description="ASP.NET Core, remote", target="Turkey", date_posted=date.today()),
        Job(source="j", site="linkedin", title="Backend Developer", url="https://x/2",
            description="C# remote", date_posted=date.today()),
    ]
    ingest(db, Evaluator(CFG), r)
    return db


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)


def test_pipeline_ai_dead_still_sends_everything(tmp_path):
    db = _db_with_jobs(tmp_path)
    before = {r["id"]: r["score"] for r in db.queued(100)}
    out = ai_review_step(db, reviewer(Resp(403)), CFG)
    assert out["reviewed"] == 0
    assert {r["id"]: r["score"] for r in db.queued(100)} == before  # untouched
    tg = FakeTG()
    assert send_queued(db, tg, CFG, dry_run=False) == len(before)
    assert all("🤖" not in t for t in tg.sent)


def test_pipeline_ai_veto_and_card(tmp_path):
    db = _db_with_jobs(tmp_path)
    vetoed = json.dumps({"fit": 9, "feasible": "no", "reason": "must live in Germany", "flags": []})
    out = ai_review_step(db, reviewer(ok(GOOD), ok(vetoed)), CFG)
    assert out["reviewed"] == 2 and out["vetoed"] == 1
    tg = FakeTG()
    send_queued(db, tg, CFG, dry_run=False)
    assert len(tg.sent) == 1 and "🤖 <b>8/10</b>" in tg.sent[0]
    # reviewed jobs are not reviewed again
    assert db.ai_candidates(0, 100) == []


def test_pipeline_survives_reviewer_bug(tmp_path):
    db = _db_with_jobs(tmp_path)

    class Broken(AIReviewer):
        def review(self, job):
            raise KeyError("bug")

    r = Broken(CFG, env=ENV, session=Session(), sleep_fn=lambda s: None)
    ai_review_step(db, r, CFG)  # must not raise
    assert len(db.queued(100)) == 2


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, source TEXT NOT NULL,"
                " site TEXT NOT NULL, target TEXT, title TEXT NOT NULL, company TEXT, location TEXT,"
                " url TEXT NOT NULL, is_remote INTEGER, date_posted TEXT, salary TEXT, description TEXT,"
                " score INTEGER NOT NULL DEFAULT 0, reasons TEXT, status TEXT NOT NULL, filter_reason TEXT,"
                " first_seen TEXT NOT NULL, sent_at TEXT, tg_message_id INTEGER, updated_at TEXT)")
    con.commit()
    con.close()
    db = DB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert {"ai_review", "ai_status", "keyword_score"} <= cols



# ---------- Groq / reasoning-model specifics ----------
def test_extra_params_are_sent():
    r = reviewer(ok(GOOD))
    assert r.review(JOB).fit == 8
    body = r.s.bodies[0]
    assert body["reasoning_effort"] == "low" and body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] >= 1000


def test_provider_rejecting_extras_falls_back_without_them():
    r = reviewer(Resp(400), ok(GOOD), ok(GOOD))
    assert r.review(JOB).fit == 8                 # retried in the same call
    assert "reasoning_effort" in r.s.bodies[0] and "reasoning_effort" not in r.s.bodies[1]
    r.review(JOB)
    assert "reasoning_effort" not in r.s.bodies[2]  # not sent again this run
    assert not r.tripped


def test_400_without_extras_is_a_normal_failure():
    cfg = {**CFG, "ai": {**CFG["ai"], "extra_params": {}}}
    r = reviewer(Resp(400), cfg=cfg)
    assert r.review(JOB) is None and r.s.calls == 1


def test_out_of_tokens_is_explained(caplog):
    trunc = Resp(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    r = reviewer(trunc)
    with caplog.at_level("WARNING"):
        assert r.review(JOB) is None
    assert "ran out of tokens" in caplog.text and "<empty>" in caplog.text


def test_null_content_does_not_crash():
    r = reviewer(Resp(200, {"choices": [{"message": {"content": None, "reasoning": "hmm"}}]}))
    assert r.review(JOB) is None
