"""Arbeitnow: free, official, keyless JSON API (Germany / EU / remote)."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from ..models import Job
from .base import (CollectResult, cooldown_active_until, get_state, iso_now, record_attempt,
                   record_failure, record_success, should_skip_fetch, utcnow)

log = logging.getLogger(__name__)

API = "https://www.arbeitnow.com/api/job-board-api"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str | None) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html or "")).strip()


def _created_ts(item: dict) -> int | None:
    created = item.get("created_at")
    return created if isinstance(created, (int, float)) else None


def item_to_job(item: dict) -> Job | None:
    if not item.get("url") or not item.get("title"):
        return None
    posted = None
    ts = _created_ts(item)
    if ts is not None:
        posted = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    tags = ", ".join(item.get("tags") or [])
    return Job(
        source="arbeitnow",
        site="arbeitnow",
        title=item["title"].strip(),
        url=item["url"],
        company=item.get("company_name"),
        location=item.get("location"),
        description=_strip_html(item.get("description")) + (f"\nTags: {tags}" if tags else ""),
        is_remote=bool(item.get("remote")),
        date_posted=posted,
        target="Arbeitnow",
    )


def collect(cfg: dict, session: requests.Session | None = None, db=None, sleep_fn=time.sleep) -> CollectResult:
    acfg = (cfg.get("extra_sources") or {}).get("arbeitnow") or {}
    result = CollectResult()
    if not acfg.get("enabled", False):
        return result

    source = "arbeitnow"
    state = get_state(db, source)
    min_interval = acfg.get("min_fetch_interval_hours", 1)
    skip, reason = should_skip_fetch(state, min_interval)
    if skip:
        result.skip(source, reason or "skipped", cooldown_until=cooldown_active_until(state),
                    details={"source_state": "skipped"})
        log.info("arbeitnow skipped: %s", reason)
        return result

    session = session or requests.Session()
    hours_old = int(((cfg.get("scrape") or {}).get("hours_old")) or 72)
    cutoff_ts = (utcnow() - timedelta(hours=hours_old)).timestamp() if hours_old > 0 else None
    pages = max(1, int(acfg.get("pages", 3)))
    max_pages = max(1, int(acfg.get("max_pages", pages)))
    pages = min(pages, max_pages)
    max_items = int(acfg.get("max_items", 3000))
    cooldown_minutes = int(acfg.get("cooldown_minutes_on_error", 30))
    url: str | None = API
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    error = None
    calls = 0
    raw_found = 0
    newest_ts = None
    stopped_early = False
    started = time.perf_counter()
    record_attempt(db, source)

    for _ in range(pages):
        if not url:
            break
        calls += 1
        try:
            r = session.get(url, timeout=30, headers={"User-Agent": "job-radar/1.0"})
            r.raise_for_status()
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            log.warning("arbeitnow failed: %s", error)
            break
        items = payload.get("data") or []
        raw_found += len(items)
        page_oldest = None
        for item in items:
            ts = _created_ts(item)
            if ts is not None:
                newest_ts = max(newest_ts or ts, ts)
                page_oldest = min(page_oldest or ts, ts)
            if cutoff_ts is not None and ts is not None and ts < cutoff_ts:
                continue
            job = item_to_job(item)
            if not job or job.url in seen_urls:
                continue
            seen_urls.add(job.url)
            jobs.append(job)
            if len(jobs) >= max_items:
                stopped_early = True
                break
        if stopped_early:
            break
        next_url = (payload.get("links") or {}).get("next")
        if cutoff_ts is not None and page_oldest is not None and page_oldest < cutoff_ts:
            stopped_early = True
            break
        if not next_url or next_url == url:
            break
        url = next_url
        if sleep_fn and pages > 1:
            try:
                sleep_fn(0)
            except Exception:  # noqa: BLE001
                pass

    duration = round(time.perf_counter() - started, 3)
    details = {"pages_requested_max": pages, "raw_found": raw_found, "unique_in_response": len(jobs),
               "stopped_early": stopped_early, "hours_old": hours_old}
    # record a single source-run entry via CollectResult semantics used by pipeline
    h = result.health[source]
    h.calls = calls
    h.found = len(jobs)
    h.errors = 1 if error else 0
    h.last_error = error[:500] if error else None
    h.duration_sec = duration
    h.raw_found = raw_found
    h.unique_found = len(jobs)
    h.details = details

    if error and not jobs:
        record_failure(db, source, error, cooldown_minutes=cooldown_minutes, checkpoint=(state or {}).get("checkpoint"))
    else:
        checkpoint = (state or {}).get("checkpoint") or {}
        if isinstance(checkpoint, dict):
            checkpoint = dict(checkpoint)
        else:
            checkpoint = {}
        if newest_ts is not None:
            checkpoint["last_created_at"] = newest_ts
        checkpoint["last_fetch_at"] = iso_now()
        checkpoint["last_raw_found"] = raw_found
        record_success(db, source, checkpoint=checkpoint,
                       last_error=error[:500] if error else None)

    result.jobs.extend(jobs)
    log.info("arbeitnow -> %d jobs (raw %d, calls %d)%s", len(jobs), raw_found, calls,
             f" error={error}" if error else "")
    return result
