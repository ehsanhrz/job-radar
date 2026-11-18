"""Remote-only job boards with official, keyless JSON APIs.

- Himalayas  https://himalayas.app/jobs/api/search?q=...   (attribution: himalayas.app)
- Jobicy     https://jobicy.com/api/v2/remote-jobs?tag=...
- Remotive   https://remotive.com/api/remote-jobs?search=... (attribution: remotive.com)

Their location restriction ("USA only", ["Germany","Poland"], "Anywhere") is put in
Job.location as "Remote: <restriction>", so filters.location_exclude can drop the
ones you can't take from Iran.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Callable

import requests

from ..models import Job
from .base import (CollectResult, cooldown_active_until, get_state, record_attempt,
                   record_failure, record_success, should_skip_fetch)

log = logging.getLogger(__name__)
UA = {"User-Agent": "job-radar/1.0 (personal job search)"}
TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html or "")).strip()


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):  # unix seconds (or ms)
            return datetime.fromtimestamp(v / 1000 if v > 10**11 else v, tz=timezone.utc).date()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


def _remote_location(restriction: Any) -> str:
    if isinstance(restriction, list):
        restriction = ", ".join(str(x) for x in restriction if x)
    restriction = (restriction or "").strip()
    return f"Remote: {restriction or 'Worldwide'}"


def _salary(lo: Any, hi: Any, cur: Any) -> str | None:
    try:
        lo = int(float(lo)) if lo not in (None, "") else None
        hi = int(float(hi)) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if not lo and not hi:
        return None
    amount = f"{lo:,}–{hi:,}" if lo and hi else f"{(lo or hi):,}"
    return f"{amount} {cur or ''}".strip()


# ---------------- parsers (pure, unit-tested) ----------------
def himalayas_item(it: dict) -> Job | None:
    url = it.get("applicationLink") or it.get("guid")
    if not url or not it.get("title"):
        return None
    return Job(
        source="remote_boards", site="himalayas", title=it["title"].strip(), url=url,
        company=it.get("companyName"),
        location=_remote_location(it.get("locationRestrictions")),
        description=_text(it.get("description") or it.get("excerpt")),
        is_remote=True,
        date_posted=_date(it.get("pubDate") or it.get("publishedAt")),
        salary=_salary(it.get("minSalary"), it.get("maxSalary"), it.get("currency")),
        target="Himalayas",
    )


def jobicy_item(it: dict) -> Job | None:
    if not it.get("url") or not it.get("jobTitle"):
        return None
    return Job(
        source="remote_boards", site="jobicy", title=_text(it["jobTitle"]), url=it["url"],
        company=it.get("companyName"),
        location=_remote_location(it.get("jobGeo")),
        description=_text(it.get("jobDescription") or it.get("jobExcerpt")),
        is_remote=True,
        date_posted=_date(it.get("pubDate")),
        salary=_salary(it.get("salaryMin") or it.get("annualSalaryMin"),
                       it.get("salaryMax") or it.get("annualSalaryMax"), it.get("salaryCurrency")),
        target="Jobicy",
    )


def remotive_item(it: dict) -> Job | None:
    if not it.get("url") or not it.get("title"):
        return None
    return Job(
        source="remote_boards", site="remotive", title=it["title"].strip(), url=it["url"],
        company=it.get("company_name"),
        location=_remote_location(it.get("candidate_required_location")),
        description=_text(it.get("description")),
        is_remote=True,
        date_posted=_date(it.get("publication_date")),
        salary=(it.get("salary") or None),
        target="Remotive",
    )


# ---------------- fetchers ----------------
def _get(session: requests.Session, url: str, params: dict) -> Any:
    r = session.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _himalayas(session, term: str, pages: int) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        data = _get(session, "https://himalayas.app/jobs/api/search",
                    {"q": term, "sort": "recent", "page": page})
        items = data.get("jobs") if isinstance(data, dict) else data
        if not items:
            break
        jobs += [j for j in map(himalayas_item, items) if j]
    return jobs


def _jobicy(session, term: str, pages: int) -> list[Job]:
    data = _get(session, "https://jobicy.com/api/v2/remote-jobs", {"count": 50, "tag": term})
    return [j for j in map(jobicy_item, data.get("jobs") or []) if j]


def _remotive(session, term: str, pages: int) -> list[Job]:
    data = _get(session, "https://remotive.com/api/remote-jobs", {"search": term})
    return [j for j in map(remotive_item, data.get("jobs") or []) if j]


BOARDS: dict[str, Callable[[requests.Session, str, int], list[Job]]] = {
    "himalayas": _himalayas,
    "jobicy": _jobicy,
    "remotive": _remotive,
}


def collect(cfg: dict, session: requests.Session | None = None, sleep_fn=time.sleep, db=None) -> CollectResult:
    rcfg = (cfg.get("extra_sources") or {}).get("remote_boards") or {}
    result = CollectResult()
    if not rcfg.get("enabled", False):
        return result
    session = session or requests.Session()
    terms = rcfg.get("search_terms") or [".net"]
    pages = int(rcfg.get("pages", 1))
    skip = {b: {t.lower() for t in ts or []} for b, ts in (rcfg.get("skip_terms") or {}).items()}
    intervals = rcfg.get("min_fetch_interval_hours") or {}
    cooldowns = rcfg.get("cooldown_minutes_on_error") or {}
    default_interval = float(rcfg.get("default_min_fetch_interval_hours", 1))
    default_cooldown = int(rcfg.get("default_cooldown_minutes", 20))
    for board in rcfg.get("boards") or list(BOARDS):
        fetch = BOARDS.get(board)
        if not fetch:
            log.warning("unknown remote board %r (known: %s)", board, ", ".join(BOARDS))
            continue
        state = get_state(db, board)
        try:
            min_interval = float(intervals.get(board, default_interval))
        except (TypeError, ValueError):
            min_interval = default_interval
        skip_fetch, reason = should_skip_fetch(state, min_interval)
        if skip_fetch:
            result.skip(board, reason or "skipped", cooldown_until=cooldown_active_until(state))
            log.info("%s skipped: %s", board, reason)
            continue
        started = time.perf_counter()
        record_attempt(db, board)
        board_jobs = []
        board_error = None
        calls = 0
        raw_found = 0
        for term in terms:
            if term.lower() in skip.get(board, ()):
                continue
            calls += 1
            try:
                jobs = fetch(session, term, pages)
            except Exception as e:  # noqa: BLE001 - one board failing must not stop the run
                board_error = f"{type(e).__name__}: {e}"
                log.warning("%s %r failed: %s", board, term, e)
                continue
            raw_found += len(jobs)
            board_jobs.extend(jobs)
            log.info("%s | %r -> %d jobs", board, term, len(jobs))
            sleep_fn(1.5)
        # dedupe within-board responses in this run
        seen_urls = set()
        unique_jobs = []
        for j in board_jobs:
            if j.url in seen_urls:
                continue
            seen_urls.add(j.url)
            unique_jobs.append(j)
        duration = round(time.perf_counter() - started, 3)
        h = result.health[board]
        h.calls = calls
        h.found = len(unique_jobs)
        h.raw_found = raw_found
        h.unique_found = len(unique_jobs)
        h.errors = 1 if board_error and not unique_jobs else 0
        h.last_error = board_error[:500] if board_error else None
        h.duration_sec = duration
        h.details = {"terms": len(terms), "raw_found": raw_found, "unique_in_response": len(unique_jobs)}
        if board_error and not unique_jobs:
            try:
                cd = int(cooldowns.get(board, default_cooldown))
            except (TypeError, ValueError):
                cd = default_cooldown
            record_failure(db, board, board_error, cooldown_minutes=cd)
        else:
            record_success(db, board, checkpoint={"last_raw_found": raw_found, "last_unique": len(unique_jobs)},
                           last_error=board_error[:500] if board_error else None)
        result.jobs.extend(unique_jobs)
    return result
