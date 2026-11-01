"""LinkedIn (logged out), Indeed, Bayt, Glassdoor... via the JobSpy library."""
from __future__ import annotations

import logging
import math
import random
import re
import time
from datetime import date, datetime
from typing import Any, Callable

from ..models import Job
from .base import CollectResult

log = logging.getLogger(__name__)

# sites JobSpy can't search with a free-text location / country
INDEED_LIKE = {"indeed", "glassdoor"}


def _val(v: Any) -> Any:
    """pandas gives us NaN / NaT for missing values."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if str(v) in ("NaT", "nan", "None"):
        return None
    return v


def _date(v: Any) -> date | None:
    v = _val(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _salary(row: dict) -> str | None:
    lo, hi = _val(row.get("min_amount")), _val(row.get("max_amount"))
    if lo is None and hi is None:
        return None
    cur = _val(row.get("currency")) or ""
    interval = _val(row.get("interval")) or ""
    amount = f"{int(lo):,}–{int(hi):,}" if lo and hi else f"{int(lo or hi):,}"
    return f"{amount} {cur} {('/ ' + interval) if interval else ''}".strip()


def row_to_job(row: dict, target_name: str) -> Job | None:
    url = _val(row.get("job_url_direct")) or _val(row.get("job_url"))
    title = _val(row.get("title"))
    if not url or not title:
        return None
    site = str(_val(row.get("site")) or "unknown")
    # Indeed: JobSpy only fills job_url_direct when the posting links to the company's
    # own site (apply there, no Indeed account). Use that as THE job link.
    link = _val(row.get("job_url_direct")) if site == "indeed" else None
    return Job(
        source="jobspy",
        site=site,
        title=str(title).strip(),
        url=str(link or _val(row.get("job_url")) or url),
        company=_val(row.get("company")),
        location=_val(row.get("location")),
        description=_val(row.get("description")),
        is_remote=bool(_val(row.get("is_remote"))) if _val(row.get("is_remote")) is not None else None,
        date_posted=_date(row.get("date_posted")),
        salary=_salary(row),
        target=target_name,
    )


BLOCK_MARKERS = ("429", "403", "blocked")


def _linkedin_id(url: str) -> str | None:
    m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", url or "")
    return m.group(1) if m else None


def make_linkedin_details_fn(proxies: list[str]):
    """Reuses JobSpy's own LinkedIn session + parser to fetch ONE job page.
    Returns {} on any failure (JobSpy logs the reason)."""
    from jobspy.linkedin import LinkedIn
    from jobspy.linkedin.constant import headers
    from jobspy.model import ScraperInput, Site
    from jobspy.util import RequestsRotating, create_session

    scraper = LinkedIn()
    scraper.scraper_input = ScraperInput(site_name=[Site.LINKEDIN], fetch_description=True,
                                         proxies=proxies or [], verbose=0)
    scraper.session = create_session(RequestsRotating(proxies or None, clear_cookies=True),
                                     scraper.scraper_input, headers)

    def fetch(job_id: str) -> dict:
        try:
            return scraper._fetch_details(job_id) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("linkedin details %s: %s", job_id, e)
            return {}
    return fetch


def collect(cfg: dict, proxies: list[str], scrape_fn=None, sleep_fn=time.sleep,
            want_details: Callable[[Job], bool] | None = None, details_fn=None) -> CollectResult:
    """cfg = the whole config dict. scrape_fn / details_fn are injectable for tests.

    LinkedIn is scraped in two phases to avoid 429s: search pages only (cheap),
    then job pages ONE AT A TIME with a pause, and only for jobs that are new and
    whose title passes the filters (`want_details`). A 429/403 on any site stops
    that site for the rest of the run instead of hammering it.
    """
    if scrape_fn is None:
        from jobspy import scrape_jobs as scrape_fn  # imported lazily: heavy

    scrape_cfg = cfg.get("scrape") or {}
    terms: list[str] = cfg.get("search_terms") or []
    targets: list[dict] = cfg.get("targets") or []
    delay_lo, delay_hi = scrape_cfg.get("delay_between_calls_sec", [8, 20])

    result = CollectResult()
    first_call = True
    blocked: set[str] = set()
    linkedin_jobs: list[Job] = []

    for target in targets:
        if not target.get("enabled", True):
            continue
        sites = [s for s in target.get("sites", []) if s != "arbeitnow"]
        target_terms = target.get("search_terms") or terms
        target_terms = target_terms + (target.get("extra_search_terms") or [])

        for term in target_terms:
            for site in sites:
                if site in blocked:
                    continue
                # one site per call -> clean per-source health + errors
                if not first_call:
                    sleep_fn(random.uniform(delay_lo, delay_hi))
                first_call = False

                kwargs: dict[str, Any] = dict(
                    site_name=[site],
                    search_term=term,
                    results_wanted=scrape_cfg.get("results_per_call", 25),
                    hours_old=scrape_cfg.get("hours_old", 72),
                    # LinkedIn descriptions are fetched later, slowly (see docstring)
                    fetch_description=scrape_cfg.get("fetch_description", True) and site != "linkedin",
                    verbose=0,
                )
                if target.get("is_remote"):
                    kwargs["is_remote"] = True
                if site in INDEED_LIKE:
                    if not target.get("country_indeed"):
                        log.debug("skip %s for %s: no country_indeed", site, target["name"])
                        continue
                    kwargs["country_indeed"] = target["country_indeed"]
                    if target.get("indeed_location"):
                        kwargs["location"] = target["indeed_location"]
                elif target.get("location"):
                    kwargs["location"] = target["location"]
                if proxies:
                    kwargs["proxies"] = proxies

                label = f"{site} | {target['name']} | {term!r}"
                try:
                    df = scrape_fn(**kwargs)
                except Exception as e:  # noqa: BLE001 - a broken board must not kill the run
                    log.warning("%s -> FAILED: %s", label, e)
                    result.call(site, error=f"{type(e).__name__}: {e}")
                    if any(m in str(e) for m in BLOCK_MARKERS):
                        blocked.add(site)
                    continue

                errors = (getattr(df, "attrs", {}) or {}).get("errors") or {}
                err = errors.get(site)
                jobs = []
                for row in df.to_dict("records"):
                    job = row_to_job(row, target["name"])
                    if job:
                        jobs.append(job)
                result.jobs.extend(jobs)
                if site == "linkedin":
                    linkedin_jobs.extend(jobs)
                result.call(site, found=len(jobs), error=str(err) if err else None)
                log.info("%s -> %d jobs%s", label, len(jobs), f" (error: {err})" if err else "")
                if err and any(m in str(err) for m in BLOCK_MARKERS):
                    blocked.add(site)
                    log.warning("%s is rate-limiting us: skipping it for the rest of this run", site)

    if linkedin_jobs and scrape_cfg.get("fetch_description", True):
        _linkedin_details_phase(linkedin_jobs, scrape_cfg, proxies, want_details, details_fn,
                                sleep_fn, result)
    return result


def _linkedin_details_phase(jobs: list[Job], scrape_cfg: dict, proxies: list[str],
                            want_details, details_fn, sleep_fn, result: CollectResult) -> None:
    seen_ids: set[str] = set()
    todo = []
    for job in jobs:
        jid = _linkedin_id(job.url)
        if not jid or jid in seen_ids:
            continue
        seen_ids.add(jid)
        if want_details is None or want_details(job):
            todo.append((jid, job))
    limit = int(scrape_cfg.get("linkedin_max_details", 40))
    skipped = max(0, len(todo) - limit)
    todo = todo[:limit]
    if not todo:
        return
    log.info("linkedin: fetching %d job pages slowly (%d skipped as seen/irrelevant)",
             len(todo), len(seen_ids) - len(todo) - skipped)
    try:
        fetch = details_fn or make_linkedin_details_fn(proxies)
    except Exception as e:  # noqa: BLE001
        log.warning("linkedin details unavailable: %s", e)
        return
    lo, hi = scrape_cfg.get("linkedin_detail_delay_sec", [6, 12])
    misses = got = 0
    for i, (jid, job) in enumerate(todo):
        if i:
            sleep_fn(random.uniform(lo, hi))
        details = fetch(jid)
        desc = details.get("description")
        if desc:
            job.description = str(desc)
            got += 1
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                result.call("linkedin", error="job pages throttled (429); kept title-only for the rest")
                log.warning("linkedin job pages throttled: stopping details for this run")
                break
    log.info("linkedin: got %d/%d descriptions", got, len(todo))
