"""Free official feeds of remote jobs (no keys, no anti-bot):

- Hacker News "Ask HN: Who is hiring?"  monthly thread via the HN Algolia API
- RemoteOK                               https://remoteok.com/api (latest ~100 jobs)
- We Work Remotely                       category RSS feeds

Each job gets location "Remote: <where>" when remote, so filters.location_exclude
drops "USA only" etc.; HN posts that don't say REMOTE get is_remote=False.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..models import Job
from .base import (CollectResult, cooldown_active_until, get_state, record_attempt,
                   record_failure, record_success, should_skip_fetch, utcnow)

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


def collect_hn(session: requests.Session, result: CollectResult | None = None, state: dict | None = None, *, return_meta: bool = False):
    r = _http_get(session, HN_SEARCH, params={"tags": "story,author_whoishiring", "query": "who is hiring",
                                             "hitsPerPage": 5}, timeout=30)
    r.raise_for_status()
    search = r.json()
    tid = latest_hn_thread_id(search)
    if not tid:
        raise ValueError("no 'Who is hiring?' thread found")
    r = _http_get(session, HN_ITEM.format(id=tid), timeout=60)
    r.raise_for_status()
    thread = r.json()
    children = thread.get("children") or []
    jobs = [j for j in map(parse_hn_comment, children) if j]
    meta = {"calls": 2, "raw_found": len(children), "checkpoint": {"thread_id": tid, "last_item_count": len(children)}}
    return (jobs, meta) if return_meta else jobs


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
            description=_text(it.get("description")) + (f"\nTags: {tags}" if tags else "")
            + (f"\nApply: {it['apply_url']}" if it.get("apply_url") else ""),
            date_posted=_date(it.get("epoch") or it.get("date")),
            salary=f"{int(lo):,}–{int(hi):,} USD" if lo and hi else None,
            target="RemoteOK",
        ))
    return jobs


def collect_remoteok(session: requests.Session, result: CollectResult | None = None, state: dict | None = None, *, return_meta: bool = False):
    r = _http_get(session, REMOTEOK, timeout=30, state=state, conditional=True)
    status = getattr(r, "status_code", 200)
    headers = getattr(r, "headers", {}) or {}
    if status == 304:
        jobs, meta = [], {"calls": 1, "raw_found": 0, "not_modified": True,
                          "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified")}
        return (jobs, meta) if return_meta else jobs
    r.raise_for_status()
    data = r.json()
    jobs = parse_remoteok(data)
    meta = {"calls": 1, "raw_found": max(0, len(data) - 1) if isinstance(data, list) else len(jobs),
            "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified")}
    return (jobs, meta) if return_meta else jobs


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


def collect_wwr(session: requests.Session, result: CollectResult | None = None, urls: list[str] | None = None, state: dict | None = None, *, return_meta: bool = False):
    if urls is None and isinstance(result, list):
        # extremely defensive; not expected
        urls, result = result, None
    urls = urls or []
    jobs, seen = [], set()
    calls = 0
    raw_found = 0
    not_modified_all = True if urls else False
    checkpoint = (state or {}).get("checkpoint") or {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    feed_state = checkpoint.get("feeds") or {}
    if not isinstance(feed_state, dict):
        feed_state = {}
    new_feed_state = dict(feed_state)
    for url in urls:
        calls += 1
        st = feed_state.get(url) if isinstance(feed_state.get(url), dict) else None
        r = _http_get(session, url, timeout=30, state=st, conditional=True)
        status = getattr(r, "status_code", 200)
        headers = getattr(r, "headers", {}) or {}
        if status == 304:
            new_feed_state[url] = {k: v for k, v in {
                "etag": headers.get("ETag") or (st or {}).get("etag"),
                "last_modified": headers.get("Last-Modified") or (st or {}).get("last_modified"),
            }.items() if v}
            continue
        not_modified_all = False
        r.raise_for_status()
        text = getattr(r, "text", "")
        parsed = parse_wwr_rss(text)
        raw_found += len(parsed)
        for j in parsed:
            if j.url not in seen:
                seen.add(j.url)
                jobs.append(j)
        new_feed_state[url] = {k: v for k, v in {
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "hash": hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16],
        }.items() if v}
    meta = {"calls": calls, "raw_found": raw_found, "not_modified": bool(urls) and not_modified_all and calls > 0,
            "checkpoint": {"feeds": new_feed_state}}
    return (jobs, meta) if return_meta else jobs


def _conditional_headers(state: dict | None) -> dict:
    headers = {}
    if not state:
        return headers
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    return headers


def _http_get(session: requests.Session, url: str, *, params=None, timeout=30, state: dict | None = None,
              conditional: bool = False):
    headers = dict(UA)
    if conditional:
        headers.update(_conditional_headers(state))
    return session.get(url, params=params, headers=headers, timeout=timeout)


# ---------------- entry point ----------------
def collect(cfg: dict, session: requests.Session | None = None, db=None) -> CollectResult:
    fcfg = (cfg.get("extra_sources") or {}).get("feeds") or {}
    result = CollectResult()
    if not fcfg.get("enabled", False):
        return result
    session = session or requests.Session()
    intervals = fcfg.get("min_fetch_interval_hours") or {}
    cooldowns = fcfg.get("cooldown_minutes_on_error") or {}

    def interval_for(name: str, default: float) -> float:
        try:
            return float(intervals.get(name, fcfg.get("default_min_fetch_interval_hours", default)))
        except (TypeError, ValueError):
            return default

    def cooldown_for(name: str, default: int = 30) -> int:
        try:
            return int(cooldowns.get(name, fcfg.get("default_cooldown_minutes", default)))
        except (TypeError, ValueError):
            return default

    def run_feed(name: str, fn, *, default_interval: float, conditional: bool = False):
        state = get_state(db, name)
        skip, reason = should_skip_fetch(state, interval_for(name, default_interval))
        if skip:
            result.skip(name, reason or "skipped", cooldown_until=cooldown_active_until(state))
            log.info("%s skipped: %s", name, reason)
            return
        started = time.perf_counter()
        record_attempt(db, name)
        try:
            jobs, meta = fn(state if conditional else None)
            duration = round(time.perf_counter() - started, 3)
            meta = meta or {}
            not_modified = bool(meta.pop("not_modified", False))
            result.jobs.extend(jobs)
            h = result.health[name]
            h.calls = int(meta.pop("calls", 1))
            h.found = len(jobs)
            h.errors = 0
            h.duration_sec = duration
            h.raw_found = int(meta.pop("raw_found", len(jobs)))
            h.unique_found = len(jobs)
            h.details = {"not_modified": not_modified, **meta} if (not_modified or meta) else {"not_modified": False}
            checkpoint = (state or {}).get("checkpoint") or {}
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            checkpoint.update(meta.get("checkpoint") or {})
            checkpoint["last_fetch_at"] = utcnow().isoformat(timespec="seconds")
            record_success(db, name, etag=meta.get("etag") or (state or {}).get("etag"),
                           last_modified=meta.get("last_modified") or (state or {}).get("last_modified"),
                           checkpoint=checkpoint)
            log.info("%s -> %d jobs%s", name, len(jobs), " (304/not modified)" if not_modified else "")
        except Exception as e:  # noqa: BLE001 - one feed failing must not stop the run
            duration = round(time.perf_counter() - started, 3)
            err = f"{type(e).__name__}: {e}"[:300]
            log.warning("%s failed: %s", name, err)
            h = result.health[name]
            h.calls = 1
            h.found = 0
            h.errors = 1
            h.last_error = err
            h.duration_sec = duration
            record_failure(db, name, err, cooldown_minutes=cooldown_for(name))

    if fcfg.get("hn", True):
        run_feed("hn", lambda st: collect_hn(session, result, st, return_meta=True), default_interval=6)
    if fcfg.get("remoteok", True):
        run_feed("remoteok", lambda st: collect_remoteok(session, result, st, return_meta=True), default_interval=2, conditional=True)
    wwr_urls = fcfg.get("wwr_rss") or []
    if wwr_urls:
        run_feed("wwr", lambda st: collect_wwr(session, result, wwr_urls, st, return_meta=True), default_interval=2, conditional=True)
    return result
