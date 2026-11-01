"""LinkedIn (logged out), Indeed, Bayt, Glassdoor... via the JobSpy library."""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import date, datetime
from typing import Any

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
    return Job(
        source="jobspy",
        site=str(_val(row.get("site")) or "unknown"),
        title=str(title).strip(),
        url=str(_val(row.get("job_url")) or url),
        company=_val(row.get("company")),
        location=_val(row.get("location")),
        description=_val(row.get("description")),
        is_remote=bool(_val(row.get("is_remote"))) if _val(row.get("is_remote")) is not None else None,
        date_posted=_date(row.get("date_posted")),
        salary=_salary(row),
        target=target_name,
    )


def collect(cfg: dict, proxies: list[str], scrape_fn=None, sleep_fn=time.sleep) -> CollectResult:
    """cfg = the whole config dict. scrape_fn is injectable for tests."""
    if scrape_fn is None:
        from jobspy import scrape_jobs as scrape_fn  # imported lazily: heavy

    scrape_cfg = cfg.get("scrape") or {}
    terms: list[str] = cfg.get("search_terms") or []
    targets: list[dict] = cfg.get("targets") or []
    delay_lo, delay_hi = scrape_cfg.get("delay_between_calls_sec", [8, 20])

    result = CollectResult()
    first_call = True

    for target in targets:
        if not target.get("enabled", True):
            continue
        sites = [s for s in target.get("sites", []) if s != "arbeitnow"]
        target_terms = target.get("search_terms") or terms
        target_terms = target_terms + (target.get("extra_search_terms") or [])

        for term in target_terms:
            for site in sites:
                # one site per call -> clean per-source health + errors
                if not first_call:
                    sleep_fn(random.uniform(delay_lo, delay_hi))
                first_call = False

                kwargs: dict[str, Any] = dict(
                    site_name=[site],
                    search_term=term,
                    results_wanted=scrape_cfg.get("results_per_call", 25),
                    hours_old=scrape_cfg.get("hours_old", 72),
                    fetch_description=scrape_cfg.get("fetch_description", True),
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
                    continue

                errors = (getattr(df, "attrs", {}) or {}).get("errors") or {}
                err = errors.get(site)
                jobs = []
                for row in df.to_dict("records"):
                    job = row_to_job(row, target["name"])
                    if job:
                        jobs.append(job)
                result.jobs.extend(jobs)
                result.call(site, found=len(jobs), error=str(err) if err else None)
                log.info("%s -> %d jobs%s", label, len(jobs), f" (error: {err})" if err else "")

    return result
