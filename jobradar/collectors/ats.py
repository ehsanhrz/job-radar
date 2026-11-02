"""Company ATS watcher with auto-discovery.

1. DISCOVER: any job link or text pointing at a company's own hiring system
       boards.greenhouse.io/<slug>   jobs.lever.co/<slug>
       jobs.ashbyhq.com/<slug>       apply.workable.com/<slug>
   adds that company to `ats_companies` (no list to maintain).
2. WATCH: companies not checked in the last `check_interval_hours` have their
   public job APIs read (no keys, no anti-bot). Only postings that would
   actually be sent (remote + full filters/score) are kept, so a company with
   500 openings never floods the DB; a board with nothing relevant N checks
   in a row is disabled.

Safety: every company is isolated (one failing board never affects another),
3 failures in a row disables a board, a 429 stops the watcher for the run,
and companies whose boards look US-only are skipped (sanctions).
"""
from __future__ import annotations

import html
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import requests

from ..db import DB, now
from ..models import Job
from .base import CollectResult

log = logging.getLogger(__name__)
UA = {"User-Agent": "job-radar/1.0 (personal job search)"}
TAG_RE = re.compile(r"<[^>]+>")

SLUG = r"([A-Za-z0-9][A-Za-z0-9_.-]{1,60})"
PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?" + SLUG, re.I),
    "lever": re.compile(r"jobs\.lever\.co/" + SLUG, re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/" + SLUG, re.I),
    "workable": re.compile(r"apply\.workable\.com/" + SLUG, re.I),
}
# path segments that aren't company boards
NOT_SLUGS = {"embed", "api", "j", "jobs", "job", "careers", "search", "v1", "share", "static", "assets"}

US_HINT = re.compile(
    r"\b(united states|usa|u\.s\.|us only|us-only|remote[ ,(-]*us\b|\bus[ -]remote|north america|"
    r"americas|canada|toronto|vancouver|"
    r"new york|san francisco|seattle|austin|boston|chicago|los angeles|denver|atlanta|"
    r"[A-Z][a-z]+, (?:CA|NY|TX|WA|MA|IL|CO|GA|FL|NC|VA|PA|OR|NJ|AZ|UT|MN|OH|MI)\b)", re.I)


# ---------------- discovery ----------------
def find_boards(text: str) -> set[tuple[str, str]]:
    found = set()
    for ats, rx in PATTERNS.items():
        for m in rx.finditer(text or ""):
            slug = m.group(1).rstrip(".-_").split("?")[0]
            if slug.lower() not in NOT_SLUGS and not slug.isdigit():
                found.add((ats, slug.lower() if ats != "ashby" else slug))
    return found


def discover(db: DB, jobs: Iterable[Job]) -> int:
    added = 0
    for job in jobs:
        for ats, slug in find_boards(f"{job.url}\n{job.description or ''}"):
            cur = db.conn.execute(
                "INSERT OR IGNORE INTO ats_companies (ats, slug, company, discovered_at, discovered_via) "
                "VALUES (?, ?, ?, ?, ?)", (ats, slug, job.company, now(), job.url))
            added += cur.rowcount
    db.conn.commit()
    if added:
        log.info("ATS discovery: %d new company boards", added)
    return added


def backfill_once(db: DB) -> int:
    """First run after upgrading: discover from every job already stored."""
    if db.get_meta("ats_backfill_v1"):
        return 0
    rows = db.conn.execute("SELECT url, description, company FROM jobs").fetchall()
    jobs = [Job(source="db", site="db", title="-", url=r["url"], description=r["description"],
                company=r["company"]) for r in rows]
    n = discover(db, jobs)
    db.set_meta("ats_backfill_v1", "1")
    return n


# ---------------- parsers (one per ATS, pure) ----------------
def _plain(s: str | None) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html.unescape(html.unescape(s or "")))).strip()


def _d(v: Any) -> date | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 10**11 else v, tz=timezone.utc).date()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        return None


def _job(ats: str, slug: str, company: str | None, title: str, url: str, where: str,
         remote: bool | None, desc: str, posted: date | None) -> Job:
    where = (where or "").strip()
    if remote is None:
        remote = True if re.search(r"\bremote\b|anywhere|worldwide", where, re.I) else False
    return Job(source="ats", site=ats, title=title.strip(), url=url, company=company or slug,
               location=(f"Remote: {where or 'unspecified'}" if remote else where or None),
               is_remote=remote, description=desc[:12000], date_posted=posted,
               target=f"{company or slug} ({ats})")


def parse_greenhouse(data: dict, slug: str, company: str | None) -> list[Job]:
    out = []
    for j in data.get("jobs") or []:
        if not j.get("title") or not j.get("absolute_url"):
            continue
        out.append(_job("greenhouse", slug, j.get("company_name") or company, j["title"], j["absolute_url"],
                        (j.get("location") or {}).get("name", ""), None, _plain(j.get("content")),
                        _d(j.get("updated_at") or j.get("first_published"))))
    return out


def parse_lever(data: list, slug: str, company: str | None) -> list[Job]:
    out = []
    for j in data or []:
        if not isinstance(j, dict) or not j.get("text") or not j.get("hostedUrl"):
            continue
        cats = j.get("categories") or {}
        wt = (j.get("workplaceType") or "").lower()
        remote = True if wt == "remote" else (False if wt in ("onsite", "on-site", "hybrid") else None)
        lists = " ".join(f"{x.get('text', '')}: {_plain(x.get('content'))}" for x in j.get("lists") or [])
        out.append(_job("lever", slug, company, j["text"], j["hostedUrl"],
                        cats.get("location") or ", ".join(cats.get("allLocations") or []), remote,
                        f"{j.get('descriptionPlain') or _plain(j.get('description'))} {lists}",
                        _d(j.get("createdAt"))))
    return out


def parse_ashby(data: dict, slug: str, company: str | None) -> list[Job]:
    out = []
    for j in data.get("jobs") or []:
        if not j.get("title") or not j.get("jobUrl") or j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        wt = (j.get("workplaceType") or "").lower()
        remote = True if j.get("isRemote") or wt == "remote" else (False if wt in ("onsite", "hybrid") else None)
        out.append(_job("ashby", slug, company, j["title"], j["jobUrl"], ", ".join(x for x in locs if x),
                        remote, j.get("descriptionPlain") or _plain(j.get("descriptionHtml")),
                        _d(j.get("publishedAt"))))
    return out


def parse_workable(data: dict, slug: str, company: str | None) -> list[Job]:
    out = []
    name = data.get("name") or company
    for j in data.get("jobs") or []:
        url = j.get("url") or j.get("application_url") or j.get("shortlink")
        if not j.get("title") or not url:
            continue
        where = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        remote = True if j.get("telecommuting") or j.get("remote") else None
        out.append(_job("workable", slug, name, j["title"], url, where, remote,
                        _plain(j.get("description")), _d(j.get("published_on") or j.get("created_at"))))
    return out


ENDPOINTS: dict[str, tuple[str, Callable]] = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", parse_ashby),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", parse_workable),
}


def looks_us_based(jobs: list[Job], share: float = 0.6) -> bool:
    """>= 3 postings and most of them point at US / North American locations."""
    if len(jobs) < 3:
        return False
    us = sum(1 for j in jobs if US_HINT.search(j.location or ""))
    return us / len(jobs) >= share


# ---------------- watcher ----------------
class RateLimited(RuntimeError):
    pass


def fetch_board(session, ats: str, slug: str, company: str | None) -> list[Job]:
    url_tpl, parse = ENDPOINTS[ats]
    r = session.get(url_tpl.format(slug=slug), headers=UA, timeout=30)
    if r.status_code == 429:
        raise RateLimited(f"{ats} 429")
    r.raise_for_status()
    return parse(r.json(), slug, company)


def watch(db: DB, cfg: dict, keep: Callable[[Job], bool] | None = None,
          session=None, sleep_fn=time.sleep) -> CollectResult:
    acfg = cfg.get("ats") or {}
    result = CollectResult()
    if not acfg.get("enabled", False):
        return result
    session = session or requests.Session()
    limit = int(acfg.get("max_companies_per_run", 40))
    max_fail = int(acfg.get("disable_after_failures", 3))
    max_empty = int(acfg.get("disable_after_empty_checks", 0))
    us_share = float(acfg.get("us_share", 0.6))
    lo, hi = acfg.get("delay_sec", [1, 3])
    # a board rarely changes within a day: re-check each company at most every N hours
    hours = float(acfg.get("check_interval_hours", 0))
    due = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = db.conn.execute(
        "SELECT * FROM ats_companies WHERE disabled IS NULL AND (last_checked IS NULL OR last_checked <= ?) "
        "ORDER BY last_checked IS NOT NULL, last_checked LIMIT ?", (due, limit)).fetchall()
    for i, row in enumerate(rows):
        ats, slug = row["ats"], row["slug"]
        if ats not in ENDPOINTS:
            continue
        if i:
            sleep_fn(random.uniform(lo, hi))
        try:
            postings = fetch_board(session, ats, slug, row["company"])
        except RateLimited as e:
            result.call(ats, error=str(e))
            log.warning("ATS watcher rate-limited (%s): stopping for this run", e)
            break
        except Exception as e:  # noqa: BLE001 - one company's board never affects another
            fails = row["failures"] + 1
            reason = f"{fails} failures ({type(e).__name__})" if fails >= max_fail else None
            db.conn.execute("UPDATE ats_companies SET failures=?, last_checked=?, disabled=? "
                            "WHERE ats=? AND slug=?", (fails, now(), reason, ats, slug))
            db.conn.commit()
            result.call(ats, error=f"{slug}: {type(e).__name__}: {e}"[:300])
            continue

        disabled = None
        if acfg.get("skip_us_companies", True) and looks_us_based(postings, us_share):
            disabled = "looks US-based (sanctions)"
        kept = [] if disabled else [
            j for j in postings if j.is_remote is not False and (keep is None or keep(j))]
        empty = 0 if kept else (row["empty_checks"] or 0) + 1
        if not disabled and max_empty and empty >= max_empty:
            disabled = f"nothing relevant in {empty} checks"
        db.conn.execute(
            "UPDATE ats_companies SET last_checked=?, last_found=?, last_kept=?, failures=0, disabled=?, "
            "empty_checks=?, company=COALESCE(?, company) WHERE ats=? AND slug=?",
            (now(), len(postings), len(kept), disabled, empty,
             postings[0].company if postings else None, ats, slug))
        db.conn.commit()
        result.jobs.extend(kept)
        result.call(ats, found=len(kept))
        log.info("ATS %s:%s -> %d postings, %d kept%s", ats, slug, len(postings), len(kept),
                 f" [{disabled}]" if disabled else "")
    return result


def run_step(db: DB, cfg: dict, collected: CollectResult, keep: Callable[[Job], bool] | None,
             session=None, sleep_fn=time.sleep) -> CollectResult:
    """Discovery + watch, fully isolated: any error here leaves the run untouched."""
    acfg = cfg.get("ats") or {}
    if not acfg.get("enabled", False):
        return CollectResult()
    try:
        if acfg.get("discover", True):
            backfill_once(db)
            discover(db, collected.jobs)
        return watch(db, cfg, keep, session=session, sleep_fn=sleep_fn)
    except Exception:  # noqa: BLE001
        log.exception("ATS step failed; continuing without it")
        return CollectResult()


def summary(db: DB) -> dict:
    q = db.conn.execute
    total = q("SELECT COUNT(*) c FROM ats_companies").fetchone()["c"]
    watched = q("SELECT COUNT(*) c FROM ats_companies WHERE disabled IS NULL").fetchone()["c"]
    by_reason = {r["disabled"]: r["c"] for r in q(
        "SELECT disabled, COUNT(*) c FROM ats_companies WHERE disabled IS NOT NULL GROUP BY disabled")}
    top = [dict(r) for r in q(
        "SELECT ats, slug, company, last_found, last_kept, last_checked FROM ats_companies "
        "WHERE disabled IS NULL ORDER BY COALESCE(last_kept,0) DESC, discovered_at DESC LIMIT 25")]
    return {"total": total, "watched": watched, "disabled": by_reason, "top": top}
