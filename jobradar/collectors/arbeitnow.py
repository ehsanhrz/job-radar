"""Arbeitnow: free, official, keyless JSON API (Germany / EU / remote)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from ..models import Job
from .base import CollectResult

log = logging.getLogger(__name__)

API = "https://www.arbeitnow.com/api/job-board-api"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str | None) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html or "")).strip()


def item_to_job(item: dict) -> Job | None:
    if not item.get("url") or not item.get("title"):
        return None
    created = item.get("created_at")
    posted = None
    if isinstance(created, (int, float)):
        posted = datetime.fromtimestamp(created, tz=timezone.utc).date()
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


def collect(cfg: dict, session: requests.Session | None = None) -> CollectResult:
    acfg = (cfg.get("extra_sources") or {}).get("arbeitnow") or {}
    result = CollectResult()
    if not acfg.get("enabled", False):
        return result

    session = session or requests.Session()
    url: str | None = API
    jobs: list[Job] = []
    pages = int(acfg.get("pages", 3))
    error = None
    for _ in range(pages):
        if not url:
            break
        try:
            r = session.get(url, timeout=30, headers={"User-Agent": "job-radar/1.0"})
            r.raise_for_status()
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            log.warning("arbeitnow failed: %s", error)
            break
        for item in payload.get("data") or []:
            job = item_to_job(item)
            if job:
                jobs.append(job)
        url = (payload.get("links") or {}).get("next")

    result.jobs.extend(jobs)
    result.call("arbeitnow", found=len(jobs), error=error)
    log.info("arbeitnow -> %d jobs", len(jobs))
    return result
