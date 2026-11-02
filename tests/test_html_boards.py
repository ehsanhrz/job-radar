"""Parsers tested against real pages saved on 2026-11-01 (tests/fixtures/*.html.gz)."""
from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import pytest
import yaml

from jobradar.collectors import html_boards as hb
from jobradar.filters import Evaluator

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 1)


def page(name: str) -> str:
    return gzip.decompress((FIX / f"{name}.html.gz").read_bytes()).decode("utf-8")


# ---------------- Kariyer.net ----------------
def test_kariyer_list_parses_real_page():
    jobs = hb.parse_kariyer_list(page("kariyer_search"), today=TODAY)
    # 53 cards on the page, 7 of them are Kariyer.net's own event ads ("Mülakat 101"...)
    assert len(jobs) == 46
    assert not any("kariyer-etkinlikleri" in j.url for j in jobs)
    assert all(j.url.startswith("https://www.kariyer.net/is-ilani/") for j in jobs)
    assert all(j.title and j.company for j in jobs)
    remote = [j for j in jobs if j.is_remote]
    onsite = [j for j in jobs if j.is_remote is False]
    assert len(remote) == 4 and len(onsite) == 42           # 32 İş Yerinde + 10 Hibrit
    assert any("İ" in j.title or "ı" in j.title for j in jobs)  # Turkish characters intact
    royal = next(j for j in jobs if "royal-luxury" in j.url)
    assert royal.title == "Web Yazılım Uzmanı" and royal.company == "The Royal Luxury Hotel"
    assert royal.location == "İstanbul(Avr.), İş Yerinde" and royal.date_posted == date(2026, 10, 30)


@pytest.mark.parametrize("text,days", [("Bugün", 0), ("3 saat", 0), ("Dün", 1), ("2 gün", 2),
                                      ("1 hafta", 7), ("2 ay", 60), ("update 18 gün", 18)])
def test_kariyer_relative_dates(text, days):
    assert hb._kariyer_age(text, TODAY) == date.fromordinal(TODAY.toordinal() - days)


def test_kariyer_onsite_dropped_remote_kept():
    ev = Evaluator(CFG)
    jobs = hb.parse_kariyer_list(page("kariyer_search"), today=TODAY)
    for j in jobs:
        if j.is_remote is False:
            assert ev.evaluate(j, today=TODAY).status == "filtered"


def test_kariyer_detail_prefers_jsonld():
    html = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Kıdemli .NET Geliştirici",'
            '"description":"<p>C# ve <b>.NET 8</b> deneyimi</p>","datePosted":"2026-10-30",'
            '"hiringOrganization":{"name":"Acme"},"jobLocationType":"TELECOMMUTE"}</script>')
    d = hb.parse_kariyer_detail(html)
    assert d["description"] == "C# ve .NET 8 deneyimi" and d["company"] == "Acme" and d["is_remote"]


# ---------------- Jaabz ----------------
def test_jaabz_list_parses_real_cards():
    jobs = hb.parse_jaabz_list(page("jaabz_remote"))
    assert len(jobs) == 12 and all(j.is_remote for j in jobs)
    devops = next(j for j in jobs if "294000" in j.url)
    assert devops.title == "DevOps Engineer (EMEA Region)" and devops.company == "OffSec"
    assert devops.location == "Remote: Global" and "Gitlab Ci" in devops.description
    poland = next(j for j in jobs if j.company == "Bending Spoons")
    assert poland.location == "Remote: Poland"


def test_jaabz_location_filter():
    ev = Evaluator(CFG)
    jobs = {j.company: j for j in hb.parse_jaabz_list(page("jaabz_remote"))}
    poland, glob = jobs["Bending Spoons"], jobs["OffSec"]
    poland.title = glob.title = "Senior Backend Engineer (.NET)"   # isolate the location rule
    # "Remote: Poland" = remote within Poland -> dropped; "Remote: Global" kept
    assert ev.evaluate(poland).filter_reason.startswith("location:")
    assert ev.evaluate(glob).status != "filtered"


def test_jaabz_fallback_without_cards():
    html = ('<a href="https://jaabz.com/jobs/1-senior-net-engineer">Senior .NET Engineer</a>'
            '<a href="https://jaabz.com/jobs/1-senior-net-engineer">View job</a>'
            '<a href="https://jaabz.com/jobs/2-backend-developer-c">x</a>')
    jobs = hb.parse_jaabz_list(html)
    assert [j.title for j in jobs] == ["Senior .NET Engineer", "x"]


def test_jaabz_detail_real_page():
    d = hb.parse_jaabz_detail(page("jaabz_detail"))
    assert d["title"] == "Senior Software Engineer" and d["company"] == "Circle"
    assert d["is_remote"] is True and d["date_posted"] == date(2026, 11, 1)
    assert "Circle (NYSE: CRCL)" in d["description"] and len(d["description"]) > 1000


def test_jaabz_ai_md_url():
    assert hb.jaabz_ai_md_url("https://jaabz.com/jobs/293813-senior-software-engineer") == \
        "https://jaabz.com/jobs/293813/ai.md"


# ---------------- collect(): details only for new, relevant, not-on-site jobs ----------------
class FakeSession:
    def __init__(self, pages: dict[str, tuple[int, str]]):
        self.pages, self.calls = pages, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        status, text = self.pages.get(url, (404, ""))

        class R:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")
        r = R()
        r.text = text
        return r


def test_collect_fetches_details_selectively():
    kariyer_list = "https://k/list"
    sess = FakeSession({kariyer_list: (200, page("kariyer_search"))})
    cfg = {"extra_sources": {"kariyer": {"enabled": True, "list_urls": [kariyer_list],
                                         "max_details": 5, "detail_delay_sec": [0, 0]}}}
    res = hb.collect(cfg, want_details=lambda j: True, session=sess, sleep_fn=lambda s: None)
    assert res.health["kariyer"].found == 46
    detail_calls = [c for c in sess.calls if c != kariyer_list]
    # only remote/unknown jobs get their page opened, never on-site ones
    onsite = {j.url for j in res.jobs if j.is_remote is False}
    assert detail_calls and not (set(detail_calls) & onsite)


def test_collect_blocked_site_is_skipped(monkeypatch):
    monkeypatch.setattr(hb, "RETRY_BLOCKED_AFTER", (0, 0))
    sess = FakeSession({"https://j/1": (403, "Forbidden")})
    cfg = {"extra_sources": {"jaabz": {"enabled": True, "list_urls": ["https://j/1", "https://j/2"]}}}
    res = hb.collect(cfg, session=sess, sleep_fn=lambda s: None)
    # retried once, then the whole site is skipped for the run (j/2 never requested)
    assert res.health["jaabz"].errors == 1 and sess.calls == ["https://j/1", "https://j/1"]


def test_blocked_once_then_ok_is_retried(monkeypatch):
    monkeypatch.setattr(hb, "RETRY_BLOCKED_AFTER", (0, 0))

    class Flaky:
        calls = 0

        def get(self, url, headers=None, timeout=None):
            Flaky.calls += 1
            status = 403 if Flaky.calls == 1 else 200
            return type("R", (), {"status_code": status, "content": b"<html>ok</html>",
                                  "text": "<html>ok</html>", "raise_for_status": lambda self: None})()

    assert hb._get(Flaky(), "https://j/x") == "<html>ok</html>" and Flaky.calls == 2


def test_kariyer_net_in_title_is_not_dotnet():
    from jobradar.models import Job
    ev = Evaluator(CFG)
    j = Job(source="x", site="kariyer", title="Kariyer.Net'le İş Hayatına İlk Adım", url="u",
            is_remote=True, description="uzaktan")
    assert ev.evaluate(j).filter_reason == "title: no include match"
    j.title = "ASP.NET Core Developer"
    assert ev.evaluate(j).status != "filtered"


def test_undeclared_charset_decoded_as_utf8():
    class R:
        status_code = 200
        encoding = "ISO-8859-1"
        content = "Kıdemli Yazılım Geliştirici".encode("utf-8")

        def raise_for_status(self):
            pass

        @property
        def text(self):
            return self.content.decode(self.encoding)

    class S:
        def get(self, *a, **k):
            return R()
    assert hb._get(S(), "u") == "Kıdemli Yazılım Geliştirici"


# ---------------- browser impersonation (Kariyer.net 403s plain requests) ----------------
def test_make_session_uses_curl_cffi_when_configured(monkeypatch):
    import sys
    import types
    made = {}

    class FakeCffiSession:
        def __init__(self, impersonate=None):
            made["impersonate"] = impersonate
    FakeCffiSession.__module__ = "curl_cffi.requests.session"
    fake_pkg = types.ModuleType("curl_cffi")
    fake_req = types.ModuleType("curl_cffi.requests")
    fake_req.Session = FakeCffiSession
    fake_pkg.requests = fake_req
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_pkg)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_req)
    s = hb.make_session({"impersonate": "chrome"})
    assert isinstance(s, FakeCffiSession) and made["impersonate"] == "chrome"
    assert type(hb.make_session({})).__module__.startswith("requests")


def test_make_session_falls_back_without_curl_cffi(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # import fails
    assert type(hb.make_session({"impersonate": "chrome"})).__module__.startswith("requests")


def test_impersonating_session_keeps_browser_user_agent():
    seen = {}

    class Resp:
        status_code = 200
        content = b"ok"

        def raise_for_status(self):
            pass

    class CffiLike:
        def get(self, url, headers=None, timeout=None):
            seen.update(headers)
            return Resp()
    CffiLike.__module__ = "curl_cffi.requests.session"
    assert hb._get(CffiLike(), "u") == "ok"
    assert "User-Agent" not in seen and "Accept-Language" in seen


def test_config_impersonates_for_kariyer_only():
    src = CFG["extra_sources"]
    assert src["kariyer"]["impersonate"] == "chrome"
    assert not src["jaabz"].get("impersonate")


# ---------------- Jaabz search layout (article.jc) ----------------
def test_jaabz_search_page_real_cards():
    jobs = hb.parse_jaabz_list(page("jaabz_search"))
    # 24 cards: 17 normal + 7 premium-locked (no link, hidden company) -> 17; no stray links
    assert len(jobs) == 17
    assert all(j.is_remote is True and j.company and j.title for j in jobs)
    assert not any(j.company == "Hidden company" for j in jobs)
    first = next(j for j in jobs if "294065" in j.url)
    assert first.title == "Senior .NET Developer" and first.company == "Integer Consulting"
    assert first.location == "Remote: Portugal" and ".Net" in first.description


def test_jaabz_search_country_filter():
    ev = Evaluator(CFG)
    jobs = hb.parse_jaabz_list(page("jaabz_search"))
    us = [j for j in jobs if j.location == "Remote: United States"]
    assert us and all(ev.evaluate(j).filter_reason.startswith("location:") for j in us)


def test_kariyer_csharp_search_term_replaced():
    urls = CFG["extra_sources"]["kariyer"]["list_urls"]
    assert not any("c%23" in u for u in urls)
