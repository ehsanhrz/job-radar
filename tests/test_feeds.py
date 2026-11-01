"""HN Who is hiring / RemoteOK / We Work Remotely, with payloads shaped like the real APIs."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import requests
import yaml

from jobradar.collectors import feeds
from jobradar.filters import Evaluator

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

HN_SEARCH = {"hits": [
    {"objectID": "45700001", "title": "Ask HN: Who wants to be hired? (November 2026)"},
    {"objectID": "45700000", "title": "Ask HN: Who is hiring? (November 2026)"},
]}
HN_THREAD = {"id": 45700000, "children": [
    {"id": 1, "author": "acme_hr", "created_at_i": 1793534400,
     "text": "Acme Pay | Senior Backend Engineer (.NET) | REMOTE (Worldwide) | Full-time | $90k-$130k"
             "<p>We build payment rails. C#, .NET 8, PostgreSQL, Kafka.<p>Apply: jobs@acmepay.example"},
    {"id": 2, "author": "bigco", "created_at_i": 1793534400,
     "text": "BigCo | Backend Engineer | ONSITE New York | Full-time<p>Java and Go."},
    {"id": 3, "author": "usco", "created_at_i": 1793534400,
     "text": "UsCo | Senior C# Developer | Remote (US only) | $150k<p>.NET shop."},
    {"id": 4, "author": "someone", "created_at_i": 1793534400,
     "text": "Is anyone hiring juniors this month?"},
    {"id": 5, "author": "whoishiring", "text": "Please only post if you are hiring..."},
]}

REMOTEOK = [
    {"legal": "API Terms of Service: please link back to Remote OK"},
    {"id": "1", "position": "Senior .NET Engineer", "company": "Globex", "location": "Worldwide",
     "url": "https://remoteok.com/remote-jobs/1", "apply_url": "https://globex.example/apply",
     "description": "<p>C# and <b>Postgres</b></p>", "tags": ["c#", "backend"], "epoch": 1793534400,
     "salary_min": 80000, "salary_max": 120000},
    {"id": "2", "position": "Backend Engineer", "company": "Initech", "location": "USA",
     "url": "https://remoteok.com/remote-jobs/2", "description": "Go", "epoch": 1793534400},
]

WWR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>WWR</title>
<item>
  <title>Hooli: Senior Backend Engineer (C#/.NET)</title>
  <region>Anywhere in the World</region>
  <link>https://weworkremotely.com/remote-jobs/hooli-senior-backend</link>
  <pubDate>Sun, 01 Nov 2026 10:00:00 +0000</pubDate>
  <description>&lt;p&gt;.NET 8, PostgreSQL, Kafka, fully remote&lt;/p&gt;</description>
</item>
<item>
  <title>Vandelay: Backend Developer</title>
  <region>USA Only</region>
  <link>https://weworkremotely.com/remote-jobs/vandelay-backend</link>
  <pubDate>Sun, 01 Nov 2026 09:00:00 +0000</pubDate>
  <description>Python</description>
</item>
</channel></rss>"""


# ---------------- HN ----------------
def test_hn_thread_picks_who_is_hiring_not_who_wants():
    assert feeds.latest_hn_thread_id(HN_SEARCH) == "45700000"


def test_hn_comments_parsed():
    jobs = [j for j in map(feeds.parse_hn_comment, HN_THREAD["children"]) if j]
    assert [j.company for j in jobs] == ["Acme Pay", "BigCo", "UsCo"]   # chatter + mod note skipped
    acme, big, us = jobs
    assert acme.is_remote and acme.location.startswith("Remote:") and "jobs@acmepay" in acme.description
    assert acme.url == "https://news.ycombinator.com/item?id=1" and acme.date_posted == date(2026, 11, 1)
    assert big.is_remote is False
    assert us.is_remote is True


def test_hn_filtering():
    ev = Evaluator(CFG)
    acme, big, us = [j for j in map(feeds.parse_hn_comment, HN_THREAD["children"]) if j]
    assert ev.evaluate(acme, today=date(2026, 11, 1)).status == "queued"   # .NET, worldwide remote
    assert ev.evaluate(big, today=date(2026, 11, 1)).status == "filtered"  # on-site
    assert ev.evaluate(us, today=date(2026, 11, 1)).status == "filtered"   # US only


# ---------------- RemoteOK ----------------
def test_remoteok_parsed_and_legal_notice_skipped():
    jobs = feeds.parse_remoteok(REMOTEOK)
    assert len(jobs) == 2
    g = jobs[0]
    assert g.title == "Senior .NET Engineer" and g.location == "Remote: Worldwide" and g.is_remote
    assert "Postgres" in g.description and "Tags: c#, backend" in g.description
    assert g.salary.startswith("80,000–120,000")
    assert Evaluator(CFG).evaluate(jobs[1]).filter_reason.startswith("location:")   # USA


# ---------------- We Work Remotely ----------------
def test_wwr_rss_parsed():
    jobs = feeds.parse_wwr_rss(WWR)
    assert [(j.company, j.title) for j in jobs] == [("Hooli", "Senior Backend Engineer (C#/.NET)"),
                                                    ("Vandelay", "Backend Developer")]
    h = jobs[0]
    assert h.location == "Remote: Anywhere in the World" and h.date_posted == date(2026, 11, 1)
    assert ".NET 8" in h.description and "<p>" not in h.description
    ev = Evaluator(CFG)
    assert ev.evaluate(h, today=date(2026, 11, 1)).status == "queued"
    assert ev.evaluate(jobs[1], today=date(2026, 11, 1)).filter_reason.startswith("location:")


# ---------------- collect(): each feed fails independently ----------------
class Resp:
    def __init__(self, payload=None, text=""):
        self._p, self.text = payload, text

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class Session:
    def get(self, url, params=None, headers=None, timeout=None):
        if "search_by_date" in url:
            return Resp(HN_SEARCH)
        if "/items/" in url:
            return Resp(HN_THREAD)
        if "remoteok" in url:
            raise requests.ConnectionError("down")
        return Resp(text=WWR)


def test_collect_all_feeds_one_down():
    cfg = {"extra_sources": {"feeds": {"enabled": True, "wwr_rss": ["https://wwr/a.rss", "https://wwr/b.rss"]}}}
    res = feeds.collect(cfg, session=Session())
    assert res.health["hn"].found == 3 and res.health["remoteok"].errors == 1
    assert res.health["wwr"].found == 2          # same items in both feeds -> deduped
    assert {j.site for j in res.jobs} == {"hn", "wwr"}


def test_feeds_enabled_in_config():
    f = CFG["extra_sources"]["feeds"]
    assert f["enabled"] and f["hn"] and f["remoteok"] and len(f["wwr_rss"]) == 2
    assert {"hn", "remoteok", "wwr"} <= set(CFG["filters"]["require_remote_sites"])
    assert CFG["targets"][0]["name"] == "Worldwide (remote)"
