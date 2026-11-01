from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
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


# ---------------- serve mode ----------------
def test_schedule_resumes_from_last_run(db):
    from datetime import datetime, timedelta, timezone
    from jobradar.serve import LAST_RUN_KEY, _seconds_until_next
    assert _seconds_until_next(db, 6 * 3600, 0) == 60.0  # fresh install
    db.set_meta(LAST_RUN_KEY, (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    assert 3.9 * 3600 < _seconds_until_next(db, 6 * 3600, 0) <= 4 * 3600  # redeploy: no immediate run
    db.set_meta(LAST_RUN_KEY, (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat())
    assert _seconds_until_next(db, 6 * 3600, 0) == 60.0  # overdue: soon


def test_try_start_run_is_exclusive(monkeypatch, tmp_path):
    import threading
    from jobradar import bot, pipeline
    gate, started = threading.Event(), threading.Event()

    def fake_run_once(settings, process_updates=True):
        assert process_updates is False
        started.set()
        gate.wait(5)

    monkeypatch.setattr(pipeline, "run_once", fake_run_once)
    settings = Settings(raw=CFG, telegram_token="t", telegram_chat_id="1", telegram_proxy=None,
                        scrape_proxies=[], db_path=tmp_path / "t.db")
    assert bot.try_start_run(settings, background=True)
    assert started.wait(5)
    assert not bot.try_start_run(settings, background=True)  # /run while scheduler runs
    gate.set()
    for _ in range(50):
        if not bot._run_lock.locked():
            break
        import time; time.sleep(0.05)
    assert bot.try_start_run(settings, background=False)  # free again (gate already open)


def test_health_endpoint(tmp_path):
    import json, socket, threading, urllib.request
    from jobradar.serve import _health_server
    settings = Settings(raw=CFG, telegram_token="t", telegram_chat_id="1", telegram_proxy=None,
                        scrape_proxies=[], db_path=tmp_path / "t.db")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    threading.Thread(target=_health_server, args=(settings, port), daemon=True).start()
    import time
    for _ in range(50):
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
            break
        except OSError:
            time.sleep(0.05)
    assert json.loads(body)["ok"] is True


def test_token_not_leaked_in_errors():
    import requests
    from jobradar.telegram import Telegram, TelegramError

    class Boom:
        proxies = {}

        def post(self, url, **kw):
            raise requests.ConnectionError(f"failed {url}")

    tg = Telegram("123:SECRET", "1")
    tg.s = Boom()
    import jobradar.telegram as t
    t.time.sleep, orig = (lambda s: None), t.time.sleep
    try:
        with pytest.raises(TelegramError) as ei:
            tg.me()
    finally:
        t.time.sleep = orig
    assert "SECRET" not in str(ei.value) and "***" in str(ei.value)


# ---------------- remote boards ----------------
def test_remote_board_parsers_and_location_filter(ev):
    from jobradar.collectors.remote_boards import himalayas_item, jobicy_item, remotive_item
    h = himalayas_item({"title": "Senior .NET Engineer", "companyName": "Acme", "locationRestrictions": [],
                        "applicationLink": "https://himalayas.app/companies/acme/jobs/x",
                        "description": "<p>C# fintech</p>", "pubDate": 1761955200, "minSalary": 60000,
                        "maxSalary": 80000, "currency": "USD"})
    assert h.location == "Remote: Worldwide" and h.description == "C# fintech" and h.salary.startswith("60,000")
    j = jobicy_item({"url": "https://jobicy.com/jobs/1", "jobTitle": "Backend Developer (C#)",
                     "companyName": "B", "jobGeo": "USA", "pubDate": "2026-10-30T12:00:00+00:00",
                     "jobDescription": "<b>.NET</b>"})
    assert j.location == "Remote: USA" and j.date_posted.isoformat() == "2026-10-30"
    r = remotive_item({"url": "https://remotive.com/x", "title": "C# Engineer", "company_name": "C",
                       "candidate_required_location": "Europe, Turkey", "publication_date": "2026-10-31T10:00:00",
                       "description": "remote"})
    assert r.location == "Remote: Europe, Turkey"
    # USA-only is dropped, worldwide and Europe/Turkey are kept
    assert ev.evaluate(j).filter_reason.startswith("location:")
    h.date_posted = r.date_posted = TODAY
    assert ev.evaluate(h).status != "filtered"
    assert ev.evaluate(r).status != "filtered"
    # normal job-board locations are untouched by the remote rule
    assert ev.evaluate(job("Senior .NET Developer", "C#", location="Austin, TX")).status != "filtered"
    for bad in ({}, {"title": "x"}, {"url": "u"}):
        assert himalayas_item(bad) is None and jobicy_item(bad) is None and remotive_item(bad) is None


def test_remote_boards_collector_survives_failures():
    from jobradar.collectors import remote_boards

    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            if "jobicy" in url:
                raise requests.ConnectionError("down")

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    if "himalayas" in url:
                        return {"jobs": [{"title": "C# Dev", "applicationLink": f"https://h/{params['q']}"}]} \
                            if params["page"] == 1 else {"jobs": []}
                    return {"jobs": [{"title": "C# Dev", "url": "https://r/1"}]}
            return R()

    import requests
    cfg = {"extra_sources": {"remote_boards": {"enabled": True, "search_terms": ["c#"], "pages": 2}}}
    res = remote_boards.collect(cfg, session=S(), sleep_fn=lambda s: None)
    assert res.health["jobicy"].errors == 1
    assert res.health["himalayas"].found == 1 and res.health["remotive"].found == 1


# ---------------- bug fixes from first real deploy ----------------
def _settings(tmp_path):
    return Settings(raw=CFG, telegram_token="t", telegram_chat_id="42", telegram_proxy=None,
                    scrape_proxies=[], db_path=tmp_path / "t.db")


def test_button_for_unknown_job_is_reported(db, tmp_path):
    from jobradar.bot import handle_update
    tg = FakeTG()
    cb = {"callback_query": {"id": "c", "data": "st:applied:deadbeefdeadbeef",
                             "message": {"chat": {"id": 42}, "message_id": 1}}}
    handle_update(db, tg, _settings(tmp_path), cb)
    assert "not in the database" in tg.answers[-1]


def test_applied_shows_up_in_list(db, ev, tmp_path):
    from jobradar.bot import handle_update
    r = CollectResult()
    r.jobs = [job("Senior .NET Developer", "C#", target="Turkey")]
    ingest(db, ev, r)
    tg = FakeTG()
    send_queued(db, tg, CFG, dry_run=False)
    jid = r.jobs[0].id
    handle_update(db, tg, _settings(tmp_path), {"callback_query": {
        "id": "c", "data": f"st:applied:{jid}", "message": {"chat": {"id": 42}, "message_id": 1}}})
    handle_update(db, tg, _settings(tmp_path), {"message": {"chat": {"id": 42}, "text": "/applied"}})
    assert "Senior .NET Developer" in tg.sent[-1][0]


def test_run_cooldown_and_force(db, tmp_path, monkeypatch):
    from jobradar import bot
    from jobradar.db import now
    started = []
    monkeypatch.setattr(bot, "try_start_run", lambda s, background=True, **k: started.append(1) or True)
    tg = FakeTG()
    db.set_meta("last_run_at", now())
    msg = lambda t: {"message": {"chat": {"id": 42}, "text": t}}
    bot.handle_update(db, tg, _settings(tmp_path), msg("/run"), allow_run=True)
    assert started == [] and "/run force" in tg.sent[-1][0]
    bot.handle_update(db, tg, _settings(tmp_path), msg("/run force"), allow_run=True)
    assert started == [1]


def test_scheduler_respects_manual_run(db):
    """After a /run, the scheduler's next slot must move back to now + interval."""
    from datetime import datetime, timedelta, timezone
    from jobradar.serve import LAST_RUN_KEY, _seconds_until_next
    db.set_meta(LAST_RUN_KEY, (datetime.now(timezone.utc) - timedelta(hours=5, minutes=55)).isoformat())
    assert _seconds_until_next(db, 6 * 3600, 0) <= 301   # scheduled run due in ~5 min
    db.set_meta(LAST_RUN_KEY, datetime.now(timezone.utc).isoformat())  # user hit /run
    assert _seconds_until_next(db, 6 * 3600, 0) > 5.9 * 3600  # scheduler now waits ~6h


# ---------------- LinkedIn 429 handling ----------------
def _li_df(n, start=0, err=None):
    df = pd.DataFrame([{"site": "linkedin", "job_url": f"https://www.linkedin.com/jobs/view/{1000 + start + i}",
                        "title": "Senior .NET Developer" if i % 2 == 0 else "Java Developer",
                        "company": f"C{start + i}", "location": "Istanbul", "date_posted": TODAY,
                        "description": None} for i in range(n)])
    df.attrs = {"errors": {"linkedin": err} if err else {}}
    return df


def test_linkedin_search_without_descriptions_then_slow_details():
    calls, fetched, sleeps = [], [], []

    def scrape(**kw):
        calls.append(kw)
        return _li_df(4, start=len(calls) * 10)

    cfg = {"search_terms": ["a"], "scrape": {"delay_between_calls_sec": [0, 0],
                                             "linkedin_detail_delay_sec": [7, 7]},
           "targets": [{"name": "TR", "sites": ["linkedin"], "location": "Türkiye"}]}
    res = jobspy_collector.collect(
        cfg, [], scrape_fn=scrape, sleep_fn=sleeps.append,
        want_details=lambda j: "Java" not in j.title,
        details_fn=lambda jid: fetched.append(jid) or {"description": f"desc {jid}"})
    assert calls[0]["fetch_description"] is False            # search pages only
    assert fetched == ["1010", "1012"]                         # only relevant ones
    assert sleeps.count(7) == 1                                # paused between job pages
    assert {j.description for j in res.jobs if ".NET" in j.title} == {"desc 1010", "desc 1012"}


def test_linkedin_details_stop_when_throttled():
    fetched = []
    cfg = {"search_terms": ["a"], "scrape": {"delay_between_calls_sec": [0, 0]},
           "targets": [{"name": "TR", "sites": ["linkedin"], "location": "X"}]}
    res = jobspy_collector.collect(cfg, [], scrape_fn=lambda **kw: _li_df(10), sleep_fn=lambda s: None,
                                   details_fn=lambda jid: fetched.append(jid) or {})
    assert len(fetched) == 2                                   # gave up after 2 misses, not 10
    assert "throttled" in res.health["linkedin"].last_error


def test_rate_limited_site_is_skipped_for_rest_of_run():
    calls = []

    def scrape(**kw):
        calls.append(kw["site_name"][0])
        if kw["site_name"] == ["linkedin"]:
            return _li_df(1, err="response status code 429 (blocked: use a proxy)")
        df = pd.DataFrame([]); df.attrs = {"errors": {}}
        return df

    cfg = {"search_terms": ["a", "b", "c"], "scrape": {"delay_between_calls_sec": [0, 0]},
           "targets": [{"name": "TR", "sites": ["linkedin", "bayt"], "location": "X"}]}
    jobspy_collector.collect(cfg, [], scrape_fn=scrape, sleep_fn=lambda s: None,
                             details_fn=lambda jid: {})
    assert calls.count("linkedin") == 1 and calls.count("bayt") == 3
