from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from jobradar.collectors import jobspy_collector
from jobradar.collectors.arbeitnow import item_to_job
from jobradar.collectors.base import CollectResult
from jobradar.config import Settings
from jobradar.db import DB
from jobradar.filters import Evaluator, looks_turkish
from jobradar.models import Job
from jobradar.pipeline import ingest, record_health, send_queued, watchdog
from jobradar.telegram import format_job

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TODAY = date.today()


def job(title, desc="", **kw) -> Job:
    base = dict(source="jobspy", site="linkedin", url=f"https://x.test/{abs(hash(title + desc))}",
                company="Acme", location="Istanbul, TR", description=desc, date_posted=TODAY)
    base.update(kw)
    return Job(title=title, **base)


@pytest.fixture
def ev():
    return Evaluator(CFG)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit_buttons(self, chat_id, message_id, buttons):
        self.edits.append((chat_id, message_id, buttons))

    def answer_callback(self, cid, text=""):
        self.answers.append(text)


# ---------------- filters ----------------
def test_dotnet_senior_turkey_is_queued(ev):
    v = ev.evaluate(job("Senior .NET Developer", "ASP.NET Core, SQL Server, Docker, remote", target="Turkey"))
    assert v.status == "queued" and v.score >= 60, v


def test_us_only_is_dropped(ev):
    v = ev.evaluate(job("Senior C# Engineer", "Candidates must be authorized to work in the United States."))
    assert v.status == "filtered" and "hard_exclude" in v.filter_reason


def test_must_reside_in_eu_is_dropped(ev):
    v = ev.evaluate(job("Backend Engineer (.NET)", "Remote, but you must be based in the EU."))
    assert v.status == "filtered"


def test_other_stack_and_junior_dropped(ev):
    assert ev.evaluate(job("Senior Java Developer")).status == "filtered"
    assert ev.evaluate(job("Junior .NET Developer")).status == "filtered"
    assert ev.evaluate(job("React Frontend Developer")).status == "filtered"
    assert ev.evaluate(job("Marketing Specialist")).status == "filtered"


def test_mixed_stack_title_kept(ev):
    assert ev.evaluate(job("Java / .NET Backend Developer", "C#")).status != "filtered"


def test_german_required_penalised(ev):
    a = ev.evaluate(job("Senior .NET Developer", "Remote. C#."))
    b = ev.evaluate(job("Senior .NET Developer", "Remote. C#. Fließende Deutschkenntnisse erforderlich."))
    assert b.score == a.score - 25


def test_turkish_detection_and_bonus(ev):
    tr = ("Ekibimize katılacak, .NET Core ve C# deneyimli, mikroservis mimarisi bilgisi olan "
          "yazılım geliştirici arkadaş aranıyor. Uzaktan çalışma için uygundur.")
    assert looks_turkish(tr)
    v = ev.evaluate(job("Kıdemli Yazılım Geliştirici (.NET)", tr))
    assert any(r.startswith("turkish") for r in v.reasons)
    assert any(r.startswith("remote") for r in v.reasons)


def test_old_job_dropped(ev):
    v = ev.evaluate(job("Senior .NET Developer", date_posted=TODAY - timedelta(days=30)))
    assert v.filter_reason == "too old"


# ---------------- models / dedupe ----------------
def test_linkedin_tracking_params_stripped():
    a = Job(source="j", site="linkedin", title="t", url="https://www.linkedin.com/jobs/view/123?refId=abc")
    b = Job(source="j", site="linkedin", title="t", url="https://www.linkedin.com/jobs/view/123?trk=x")
    assert a.id == b.id


def test_cross_site_duplicate_skipped(db, ev):
    r = CollectResult()
    r.jobs = [
        job("Senior .NET Developer", "C#", site="linkedin", url="https://li.test/1"),
        job("Senior .NET Developer", "C#", site="indeed", url="https://in.test/9"),
    ]
    counts = ingest(db, ev, r)
    assert counts["new"] == 1 and counts["duplicate"] == 1
    # second run: everything is a dupe
    assert ingest(db, ev, r)["duplicate"] == 2


# ---------------- collectors ----------------
def test_jobspy_collector_with_fake_scraper():
    calls = []

    def fake_scrape(**kw):
        calls.append(kw)
        df = pd.DataFrame([{
            "site": kw["site_name"][0], "job_url": f"https://{kw['site_name'][0]}.test/{len(calls)}",
            "job_url_direct": None, "title": "Senior .NET Developer", "company": "Acme",
            "location": "Istanbul, TR", "date_posted": TODAY, "is_remote": True,
            "description": "C#", "min_amount": 50000.0, "max_amount": 70000.0,
            "currency": "USD", "interval": "yearly",
        }])
        df.attrs = {"errors": {"indeed": "blocked"} if kw["site_name"] == ["indeed"] else {}}
        return df

    cfg = {
        "search_terms": ["a"],
        "scrape": {"delay_between_calls_sec": [0, 0]},
        "targets": [
            {"name": "Turkey", "sites": ["linkedin", "indeed"], "location": "Türkiye", "country_indeed": "turkey"},
            {"name": "Iraq", "sites": ["indeed", "bayt"], "location": "Iraq"},  # indeed skipped: no country
        ],
    }
    res = jobspy_collector.collect(cfg, proxies=["1.2.3.4:80"], scrape_fn=fake_scrape, sleep_fn=lambda s: None)
    assert [c["site_name"] for c in calls] == [["linkedin"], ["indeed"], ["bayt"]]
    assert calls[1]["country_indeed"] == "turkey" and "location" not in calls[1]
    assert calls[0]["location"] == "Türkiye" and calls[0]["proxies"] == ["1.2.3.4:80"]
    assert len(res.jobs) == 3
    assert res.health["indeed"].errors == 1
    assert res.jobs[0].salary.startswith("50,000–70,000 USD")


def test_jobspy_collector_survives_exceptions():
    def boom(**kw):
        raise RuntimeError("429 Too Many Requests")

    cfg = {"search_terms": ["a"], "scrape": {"delay_between_calls_sec": [0, 0]},
           "targets": [{"name": "X", "sites": ["linkedin"], "location": "X"}]}
    res = jobspy_collector.collect(cfg, [], scrape_fn=boom, sleep_fn=lambda s: None)
    assert res.jobs == [] and res.health["linkedin"].errors == 1


def test_arbeitnow_item():
    j = item_to_job({"slug": "x", "company_name": "Foo GmbH", "title": "Backend Developer (C#)",
                     "description": "<p>We use <b>.NET</b></p>", "remote": True,
                     "url": "https://www.arbeitnow.com/jobs/x", "tags": ["C#"], "location": "Berlin",
                     "created_at": 1761955200})
    assert j.is_remote and ".NET" in j.description and "<" not in j.description


# ---------------- telegram flow ----------------
def test_send_queued_and_button_flow(db, ev, tmp_path):
    r = CollectResult()
    r.jobs = [job("Senior .NET Developer", "ASP.NET Core <script>", target="Turkey")]
    ingest(db, ev, r)
    tg = FakeTG()
    assert send_queued(db, tg, CFG, dry_run=False) == 1
    text, buttons = tg.sent[0]
    assert "&lt;script&gt;" not in text  # description isn't in the message at all
    assert "Senior .NET Developer" in text
    job_id = r.jobs[0].id
    assert buttons[0][0]["callback_data"] == f"st:applied:{job_id}"
    assert db.get(job_id)["status"] == "sent"

    from jobradar.bot import handle_update
    settings = Settings(raw=CFG, telegram_token="t", telegram_chat_id="42", telegram_proxy=None,
                        scrape_proxies=[], db_path=tmp_path / "t.db")
    cb = {"callback_query": {"id": "c1", "data": f"st:applied:{job_id}",
                             "message": {"chat": {"id": 42}, "message_id": 1}}}
    handle_update(db, tg, settings, cb)
    assert db.get(job_id)["status"] == "applied"
    assert tg.edits[-1][2][0][0]["text"] == "✅ Applied"

    # someone else's chat is ignored
    cb["callback_query"]["message"]["chat"]["id"] = 999
    cb["callback_query"]["data"] = f"st:skipped:{job_id}"
    handle_update(db, tg, settings, cb)
    assert db.get(job_id)["status"] == "applied"


def test_format_job_escapes_html():
    row = {"title": "C# <Senior> & Co", "company": "A&B", "location": "X", "url": "https://x/?a=1&b=2",
           "score": 50, "reasons": '["title:.net +30"]', "site": "linkedin", "target": "Turkey",
           "is_remote": 1, "salary": None, "date_posted": "2026-11-01"}
    out = format_job(row)
    assert "C# &lt;Senior&gt; &amp; Co" in out and 'href="https://x/?a=1&amp;b=2"' in out


# ---------------- watchdog ----------------
def test_watchdog_alerts_once_and_recovers(db):
    tg = FakeTG()
    cfg = {"watchdog": {"zero_runs_alert": 3}}
    empty = CollectResult()
    empty.call("linkedin", found=0, error="blocked")
    for _ in range(2):
        record_health(db, empty)
        assert watchdog(db, empty, cfg, tg) == []
    record_health(db, empty)
    assert len(watchdog(db, empty, cfg, tg)) == 1
    record_health(db, empty)
    assert watchdog(db, empty, cfg, tg) == []  # no spam
    ok = CollectResult()
    ok.call("linkedin", found=12)
    record_health(db, ok)
    msgs = watchdog(db, ok, cfg, tg)
    assert len(msgs) == 1 and "back" in msgs[0]


# ---------------- real Telegram client (HTTP layer mocked) ----------------
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.content, self.text = payload, status, b"x", str(payload)

    def json(self):
        return self._p


class _Session:
    def __init__(self, result):
        self.calls, self.result, self.proxies = [], result, {}

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json, timeout))
        return _Resp({"ok": True, "result": self.result})


def test_telegram_client_get_updates_and_send():
    from jobradar.telegram import Telegram
    tg = Telegram("TOKEN", "42")
    tg.s = _Session([])
    assert tg.get_updates(None, timeout=0) == []
    method, body, http_timeout = tg.s.calls[0]
    assert method == "getUpdates" and body["timeout"] == 0 and http_timeout == 15

    tg.s = _Session({"message_id": 7})
    assert tg.send("hi", [[{"text": "a", "callback_data": "b"}]]) == 7
    method, body, _ = tg.s.calls[0]
    assert method == "sendMessage" and body["chat_id"] == "42" and body["parse_mode"] == "HTML"
