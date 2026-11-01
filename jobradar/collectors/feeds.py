"""Free official feeds of remote jobs (no keys, no anti-bot):

- Hacker News "Ask HN: Who is hiring?"  monthly thread via the HN Algolia API
- RemoteOK                               https://remoteok.com/api (latest ~100 jobs)
- We Work Remotely                       category RSS feeds

Each job gets location "Remote: <where>" when remote, so filters.location_exclude
drops "USA only" etc.; HN posts that don't say REMOTE get is_remote=False.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..models import Job
from .base import CollectResult

log = logging.getLogger(__name__)
UA = {"User-Agent": "job-radar/1.0 (personal job search; github.com/ehsanhrz/job-radar)"}
TAG_RE = re.compile(r"<[^>]+>")

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
REMOTEOK = "https://remoteok.com/api"


def _text(s: str | None) -> str:
    s = re.sub(r"<\s*(br|p)\s*/?>", "\n", s or "", flags=re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc).date()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        try:
            return parsedate_to_datetime(str(v)).date()  # RSS pubDate
        except (TypeError, ValueError):
            return None


# ---------------- Hacker News: Who is hiring ----------------
def latest_hn_thread_id(search_json: dict) -> str | None:
    for hit in search_json.get("hits") or []:
        if re.match(r"Ask HN: Who is hiring\?", hit.get("title") or "", re.I):
            return str(hit.get("objectID"))
    return None


def parse_hn_comment(c: dict) -> Job | None:
    """Top-level comments start with a header like
    'Acme | Senior Backend Engineer | REMOTE (Worldwide) | Full-time | $120k'."""
    raw = c.get("text") or ""
    if not raw or c.get("author") in (None, "whoishiring"):
        return None
    body = _text(raw)
    header = body.split("\n", 1)[0].strip()
    if "|" not in header or len(header) > 400:
        return None  # not a job post (questions, replies, chatter)
    parts = [p.strip() for p in header.split("|") if p.strip()]
    company = parts[0][:80]
    is_remote = bool(re.search(r"\bremote\b", header, re.I))
    onsite_only = bool(re.search(r"\b(onsite|on-site|in[- ]office)\b", header, re.I)) and not is_remote
    rest = " | ".join(parts[1:])
    return Job(
        source="feeds", site="hn",
        title=rest[:200] or company,
        url=f"https://news.ycombinator.com/item?id={c.get('id')}",
        company=company,
        location=f"Remote: {rest}" if is_remote else (rest[:120] or None),
        is_remote=True if is_remote else (False if onsite_only or rest else None),
        description=body[:12000],
        date_posted=_date(c.get("created_at_i") or c.get("created_at")),
        target="HN Who is hiring",
    )


def collect_hn(session: requests.Session, result: CollectResult) -> list[Job]:
    r = session.get(HN_SEARCH, params={"tags": "story,author_whoishiring", "query": "who is hiring",
                                       "hitsPerPage": 5}, headers=UA, timeout=30)
    r.raise_for_status()
    tid = latest_hn_thread_id(r.json())
    if not tid:
        raise ValueError("no 'Who is hiring?' thread found")
    r = session.get(HN_ITEM.format(id=tid), headers=UA, timeout=60)
    r.raise_for_status()
    jobs = [j for j in map(parse_hn_comment, r.json().get("children") or []) if j]
    return jobs


# ---------------- RemoteOK ----------------
def parse_remoteok(items: list) -> list[Job]:
    jobs = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("position") or not (it.get("url") or it.get("apply_url")):
            continue  # first element is the legal notice
        where = (it.get("location") or "").strip() or "Worldwide"
        tags = ", ".join(it.get("tags") or [])
        lo, hi = it.get("salary_min"), it.get("salary_max")
        jobs.append(Job(
            source="feeds", site="remoteok",
            title=_text(it["position"]),
            url=it.get("url") or it["apply_url"],
            company=it.get("company"),
            location=f"Remote: {where}",
            is_remote=True,
            description=_text(it.get("description")) + (f"\nTags: {tags}" if tags else ""),
            date_posted=_date(it.get("epoch") or it.get("date")),
            salary=f"{int(lo):,}–{int(hi):,} USD" if lo and hi else None,
            target="RemoteOK",
        ))
    return jobs


def collect_remoteok(session: requests.Session, result: CollectResult) -> list[Job]:
    r = session.get(REMOTEOK, headers=UA, timeout=30)
    r.raise_for_status()
    return parse_remoteok(r.json())


# ---------------- We Work Remotely ----------------
def parse_wwr_rss(xml_text: str) -> list[Job]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    jobs = []
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        raw_title, link = get("title"), get("link")
        if not raw_title or not link:
            continue
        company, _, title = raw_title.partition(":")
        if not title:
            company, title = "", raw_title
        region = get("region") or get("country") or "Anywhere in the World"
        jobs.append(Job(
            source="feeds", site="wwr", title=_text(title), url=link,
            company=_text(company) or None,
            location=f"Remote: {region}", is_remote=True,
            description=_text(get("description")),
            date_posted=_date(get("pubDate")),
            target="We Work Remotely",
        ))
    return jobs


def collect_wwr(session: requests.Session, result: CollectResult, urls: list[str]) -> list[Job]:
    jobs, seen = [], set()
    for url in urls:
        r = session.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        for j in parse_wwr_rss(r.text):
            if j.url not in seen:
                seen.add(j.url)
                jobs.append(j)
    return jobs


# ---------------- entry point ----------------
def collect(cfg: dict, session: requests.Session | None = None) -> CollectResult:
    fcfg = (cfg.get("extra_sources") or {}).get("feeds") or {}
    result = CollectResult()
    if not fcfg.get("enabled", False):
        return result
    session = session or requests.Session()
    plan = []
    if fcfg.get("hn", True):
        plan.append(("hn", lambda: collect_hn(session, result)))
    if fcfg.get("remoteok", True):
        plan.append(("remoteok", lambda: collect_remoteok(session, result)))
    wwr_urls = fcfg.get("wwr_rss") or []
    if wwr_urls:
        plan.append(("wwr", lambda: collect_wwr(session, result, wwr_urls)))
    for name, fn in plan:
        try:
            jobs = fn()
        except Exception as e:  # noqa: BLE001 - one feed failing must not stop the run
            log.warning("%s failed: %s", name, e)
            result.call(name, error=f"{type(e).__name__}: {e}"[:300])
            continue
        result.jobs.extend(jobs)
        result.call(name, found=len(jobs))
        log.info("%s -> %d jobs", name, len(jobs))
    return result
