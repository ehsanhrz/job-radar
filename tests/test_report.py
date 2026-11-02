from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from jobradar.collectors.base import CollectResult
from jobradar.config import Settings
from jobradar.db import DB
from jobradar.filters import Evaluator
from jobradar.models import Job
from jobradar.pipeline import ingest, send_queued
from jobradar.report import build_report, caption, parse_period, report_json

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


class FakeTG:
    def __init__(self):
        self.sent, self.docs = [], []

    def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)

    def send_document(self, filename, data, caption=""):
        self.docs.append((filename, data, caption))
        return 1


def _seed(tmp_path) -> DB:
    db = DB(tmp_path / "r.db")
    r = CollectResult()
    r.jobs = [
        Job(source="j", site="kariyer", title="Senior .NET Developer", url="https://k/1", company="Acme",
            description="C# fintech payments, uzaktan", is_remote=True, target="Kariyer.net",
            date_posted=date.today()),
        Job(source="j", site="jaabz", title="Backend Engineer", url="https://j/2", company="B",
            description="Node", is_remote=True, location="Remote: Global", date_posted=date.today()),
        Job(source="j", site="jaabz", title="Senior Java Developer", url="https://j/3", company="C",
            is_remote=True, date_posted=date.today()),
    ]
    ingest(db, Evaluator(CFG), r)
    send_queued(db, FakeTG(), CFG, dry_run=False)
    db.set_status(r.jobs[0].id, "applied")

    # a finished run with a blocked source and AI down
    run1 = db.start_run("schedule")
    db.finish_run(run1, {"new": 3, "duplicate": 10, "filtered": 1, "low": 1, "queued": 1, "sent": 1,
                         "ai": {"reviewed": 0, "failed": 1, "off": "key rejected (HTTP 401)"}},
                  {"kariyer": {"calls": 3, "found": 40, "errors": 0, "last_error": None},
                   "linkedin": {"calls": 1, "found": 0, "errors": 1, "last_error": "429 (blocked)"}}, [])
    db.start_run("schedule")   # killed (never finished, not the latest)
    db.start_run("manual")     # latest, still running
    return db


def test_report_structure_and_issues(tmp_path):
    rep = build_report(_seed(tmp_path), CFG, "24h")
    assert set(rep) >= {"report", "funnel", "sources", "ai", "runs", "jobs", "issues", "config"}
    assert [r["state"] for r in rep["runs"]] == ["done", "killed", "running"]
    assert rep["runs"][0]["minutes"] is not None and rep["runs"][0]["trigger"] == "schedule"
    assert rep["sources"]["linkedin"]["status"] == "blocked_or_failing"
    assert rep["sources"]["kariyer"]["status"] == "ok"
    whats = " | ".join(i["what"] for i in rep["issues"])
    assert "killed" in whats and "key rejected" in whats and "0 jobs in 1 run" in whats
    assert rep["funnel"]["found"] == 40 and rep["funnel"]["sent"] == 1
    f = rep["funnel"]                                    # stages add up (post-AI, current status)
    assert f["new"] == sum(f[k] for k in ("filtered", "ai_vetoed", "low", "queued", "reached_you"))
    assert rep["jobs"]["applied_all_time"][0]["title"] == "Senior .NET Developer"
    assert any("title" in k for k in rep["funnel"]["filter_reasons"])     # the Java job
    job = rep["jobs"]["applied_all_time"][0]
    assert {"url", "score", "reasons", "ai", "status", "description_snippet"} <= set(job)


def test_report_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "gsk_SUPERSECRET")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKENSECRET")
    monkeypatch.setenv("AI_MODEL", "openai/gpt-oss-120b")
    _, data = report_json(_seed(tmp_path), CFG, "all")
    text = data.decode()
    assert "SUPERSECRET" not in text and "TOKENSECRET" not in text
    assert json.loads(text)["ai"]["model"] == "openai/gpt-oss-120b"


@pytest.mark.parametrize("arg,label,has_since", [("24h", "24h", True), ("7d", "7d", True),
                                                 ("2w", "2w", True), ("all", "all", False), (None, "24h", True)])
def test_parse_period(arg, label, has_since):
    lab, since = parse_period(arg)
    assert lab == label and (since is not None) == has_since


def test_parse_period_rejects_garbage():
    with pytest.raises(ValueError):
        parse_period("yesterday")


def test_report_command_sends_file(tmp_path):
    from jobradar.bot import handle_update
    db = _seed(tmp_path)
    tg = FakeTG()
    s = Settings(raw=CFG, telegram_token="t", telegram_chat_id="42", telegram_proxy=None,
                 scrape_proxies=[], db_path=tmp_path / "r.db")
    handle_update(db, tg, s, {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/report 7d"}})
    name, data, cap = tg.docs[-1]
    assert name.startswith("jobradar-report-7d-") and name.endswith(".json")
    assert json.loads(data)["report"]["period"] == "7d" and "issues:" in cap
    handle_update(db, tg, s, {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/report lol"}})
    assert "bad period" in tg.sent[-1]


def test_run_once_records_runs(tmp_path, monkeypatch):
    from jobradar import pipeline
    monkeypatch.setattr(pipeline, "collect_all", lambda cfg, proxies, want_details=None: CollectResult())
    s = Settings(raw={**CFG, "telegram": {"send_run_summary": False}}, telegram_token="t",
                 telegram_chat_id="42", telegram_proxy=None, scrape_proxies=[], db_path=tmp_path / "x.db")
    monkeypatch.setattr(pipeline, "Telegram", lambda *a, **k: FakeTG())
    pipeline.run_once(s, process_updates=False, trigger="manual")
    rep = build_report(DB(tmp_path / "x.db"), CFG, "1h")
    assert len(rep["runs"]) == 1 and rep["runs"][0]["state"] == "done" and rep["runs"][0]["trigger"] == "manual"


def test_caption_summarises(tmp_path):
    rep = build_report(_seed(tmp_path), CFG, "24h")
    c = caption(rep)
    assert "3 runs" in c and "errors" in c


def test_telegram_send_document_multipart():
    from jobradar.telegram import Telegram

    class R:
        content = b"x"

        def json(self):
            return {"ok": True, "result": {"message_id": 9}}

    class S:
        proxies = {}

        def post(self, url, data=None, files=None, timeout=None, json=None):
            self.url, self.data, self.files = url, data, files
            return R()

    tg = Telegram("123:SECRET", "42")
    tg.s = S()
    assert tg.send_document("r.json", b"{}", "cap") == 9
    assert tg.s.url.endswith("/sendDocument") and tg.s.data["chat_id"] == "42"
    assert tg.s.files["document"][0] == "r.json"


# ---------------- only the owner, only the owner's chat ----------------
def _s(tmp_path, chat="42", users=frozenset()):
    return Settings(raw=CFG, telegram_token="t", telegram_chat_id=chat, telegram_proxy=None,
                    scrape_proxies=[], db_path=tmp_path / "r.db", allowed_user_ids=users)


@pytest.mark.parametrize("upd", [
    {"message": {"from": {"id": 666}, "chat": {"id": 666}, "text": "/report all"}},     # stranger, own chat
    {"message": {"from": {"id": 666}, "chat": {"id": 42}, "text": "/report all"}},      # stranger, spoofed chat
    {"message": {"chat": {"id": 42}, "text": "/report all"}},                           # no sender
    {"edited_message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/report all"}},
    {"channel_post": {"chat": {"id": 42}, "text": "/report all"}},
    {"callback_query": {"from": {"id": 666}, "id": "c", "data": "st:applied:x",
                        "message": {"chat": {"id": 42}, "message_id": 1}}},
])
def test_everything_not_from_owner_is_ignored(tmp_path, upd):
    from jobradar.bot import handle_update
    tg = FakeTG()
    tg.answers = []
    tg.answer_callback = lambda cid, text="": tg.answers.append(text)
    handle_update(_seed(tmp_path), tg, _s(tmp_path), upd)
    assert tg.sent == [] and tg.docs == [] and tg.answers == []   # no reply of any kind


def test_owner_with_bot_suffix_and_args(tmp_path):
    from jobradar.bot import handle_update
    tg = FakeTG()
    handle_update(_seed(tmp_path), tg, _s(tmp_path),
                  {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/report@jobScrappe_bot 7d"}})
    assert tg.docs and json.loads(tg.docs[-1][1])["report"]["period"] == "7d"


def test_group_chat_needs_allowed_users(tmp_path):
    from jobradar.bot import is_authorized
    upd = {"message": {"from": {"id": 42}, "chat": {"id": -100123, "type": "group"}, "text": "/stats"}}
    assert not is_authorized(upd, _s(tmp_path, chat="-100123"))                        # nobody by default
    assert is_authorized(upd, _s(tmp_path, chat="-100123", users=frozenset({"42"})))
    upd["message"]["from"]["id"] = 7
    assert not is_authorized(upd, _s(tmp_path, chat="-100123", users=frozenset({"42"})))


def test_empty_chat_id_authorizes_nobody(tmp_path):
    from jobradar.bot import is_authorized
    upd = {"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/stats"}}
    assert not is_authorized(upd, _s(tmp_path, chat=""))
