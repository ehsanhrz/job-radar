"""HTML job boards: Kariyer.net (Turkey) and Jaabz (international, remote tag).

Parsers were written against real saved pages (tests/fixtures/*.html.gz).
Flow per board: listing pages -> cards (title, company, location, remote flag)
-> job pages fetched slowly, only for NEW jobs whose title passes the filters
and that aren't clearly on-site.

If a site changes its HTML, `python -m jobradar probe` shows what we parse.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import CollectResult

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "tr,en;q=0.8"}
BLOCK_STATUSES = (403, 429)


class Blocked(RuntimeError):
    pass


def make_session(bcfg: dict | None = None):
    """Plain requests, or curl_cffi impersonating a real browser's TLS fingerprint when
    the board blocks Python clients (Kariyer.net: 403 for requests, 200 for Chrome)."""
    imp = (bcfg or {}).get("impersonate")
    if imp:
        try:
            from curl_cffi import requests as cffi  # installed with python-jobspy
            return cffi.Session(impersonate=imp)
        except Exception as e:  # noqa: BLE001
            log.warning("curl_cffi unavailable (%s): using plain requests", e)
    return requests.Session()


RETRY_BLOCKED_AFTER = (4.0, 9.0)  # one retry after a short pause; occasional 403s are transient


def _get(session, url: str, retry_blocked: bool = True) -> str:
    impersonating = type(session).__module__.startswith("curl_cffi")
    # an impersonating session must keep the browser's own headers (UA must match the fingerprint)
    headers = {"Accept-Language": HEADERS["Accept-Language"]} if impersonating else HEADERS
    r = session.get(url, headers=headers, timeout=30)
    if r.status_code in BLOCK_STATUSES and retry_blocked and RETRY_BLOCKED_AFTER:
        time.sleep(random.uniform(*RETRY_BLOCKED_AFTER))
        r = session.get(url, headers=headers, timeout=30)
    if r.status_code in BLOCK_STATUSES:
        raise Blocked(f"HTTP {r.status_code} (blocked)")
    r.raise_for_status()
    content = getattr(r, "content", None)
    if isinstance(content, (bytes, bytearray)):
        try:
            return content.decode("utf-8")  # both boards are UTF-8; avoids latin-1 guesses
        except UnicodeDecodeError:
            pass
    return r.text


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


# ---------------- shared: JSON-LD JobPosting ----------------
def jsonld_jobposting(html: str) -> dict | None:
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                return it
    return None


def _jsonld_fields(jp: dict) -> dict:
    org = jp.get("hiringOrganization") or {}
    out = {
        "title": jp.get("title"),
        "company": org.get("name") if isinstance(org, dict) else None,
        "description": BeautifulSoup(jp.get("description") or "", "html.parser").get_text(" ", strip=True),
        "date_posted": _iso_date(jp.get("datePosted")),
    }
    if jp.get("jobLocationType") == "TELECOMMUTE":
        out["is_remote"] = True
    return out


def _iso_date(v: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date() if v else None
    except ValueError:
        return None


# ---------------- Kariyer.net ----------------
KARIYER = "https://www.kariyer.net"


def _kariyer_age(text: str, today: date) -> date | None:
    """Card dates are relative: 'Bugün', 'Dün', '3 saat', '2 gün', '1 hafta', '2 ay'."""
    t = (text or "").lower().replace("update", "").strip()
    if not t:
        return None
    if "bugün" in t or "saat" in t or "dakika" in t or "yeni" in t:
        return today
    if "dün" in t:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\s*(gün|hafta|ay)", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return today - timedelta(days=n * {"gün": 1, "hafta": 7, "ay": 30}[unit])


def kariyer_remote_flag(work_model: str) -> bool | None:
    w = (work_model or "").lower()
    if "uzaktan" in w or "remote" in w:
        return True
    if "hibrit" in w or "hybrid" in w or "yerinde" in w or "on-site" in w:
        return False
    return None


def parse_kariyer_list(html: str, today: date | None = None) -> list[Job]:
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for card in soup.select('a.k-ad-card, a[data-test="ad-card-item"]'):
        href = card.get("href") or ""
        if "/is-ilani/" not in href or "/is-ilani/kariyer-etkinlikleri" in href:
            continue  # Kariyer.net's own events ("Mülakat 101" etc.), not jobs
        url = href if href.startswith("http") else KARIYER + href
        if url in seen:
            continue
        seen.add(url)
        title = _text(card.select_one('[data-test="ad-card-title"]'))
        if not title:
            continue
        work_model = _text(card.select_one('[data-test="work-model"]'))
        city = _text(card.select_one('[data-test="location"]'))
        jobs.append(Job(
            source="html_boards", site="kariyer", title=title, url=url,
            company=_text(card.select_one('[data-test="subtitle"]')) or None,
            location=", ".join(x for x in (city, work_model) if x) or None,
            is_remote=kariyer_remote_flag(work_model),
            date_posted=_kariyer_age(_text(card.select_one('[data-test="ad-date"]')), today),
            target="Turkey",   # so scoring.target_bonus.Turkey applies like LinkedIn/Indeed Turkey
        ))
    return jobs


def parse_kariyer_detail(html: str) -> dict:
    jp = jsonld_jobposting(html)
    if jp:
        return _jsonld_fields(jp)
    # fallback: the job text block / meta description
    soup = BeautifulSoup(html, "html.parser")
    for sel in ('[data-test="job-detail-description"]', ".job-detail-content", "#job-detail",
                ".qualifications", "main"):
        el = soup.select_one(sel)
        if el and len(_text(el)) > 200:
            return {"description": _text(el)}
    meta = soup.find("meta", attrs={"name": "description"})
    return {"description": meta.get("content", "")} if meta else {}


# ---------------- Jaabz ----------------
JOB_URL_RE = re.compile(r"https://jaabz\.com/jobs/(\d+)-[a-z0-9-]+")


def parse_jaabz_list(html: str) -> list[Job]:
    """Two card layouts: /jobs/remote uses `article.hx-job`, search pages use `article.jc`."""
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for card in soup.select("article.hx-job, article.jc"):
        classes = card.get("class") or []
        if "premium-locked" in classes or "jc--locked" in classes:
            continue  # "Hidden company / Log in to see location", no link
        a = card.select_one(".hx-job-title a[href], .jc-title a[href]")
        href = a["href"] if a else card.get("data-job-url", "")
        if not JOB_URL_RE.match(href or ""):
            continue
        url = href.split("?")[0]
        if url in seen:
            continue
        seen.add(url)
        perks = [_text(p).lower() for p in card.select(".hx-perk, .jc-perk")]
        where = _text(card.select_one(".hx-job-where, .jc-where"))
        if where.lower().startswith("log in"):
            where = ""
        skills = [_text(li) for li in card.select(".hx-job-skills li, .jc-skills li")]
        facts = [_text(sp) for sp in card.select(".hx-job-facts span, .jc-facts span")]
        if "remote" in perks or "jc--remote" in classes:
            is_remote = True
        else:
            is_remote = False if perks else None
        jobs.append(Job(
            source="html_boards", site="jaabz", title=_text(a) if a else "", url=url,
            company=_text(card.select_one(".hx-job-company, .jc-company")) or None,
            # "Remote: <country>" lets filters.location_exclude drop e.g. "Remote: United States"
            location=f"Remote: {where}" if is_remote and where else (where or None),
            is_remote=is_remote,
            description=("Skills: " + ", ".join(skills) + ". " + " · ".join(facts)) if skills or facts else None,
            target="Jaabz",
        ))
    if jobs or soup.select("article.hx-job, article.jc"):
        return jobs  # a known layout was present: never fall back to loose links
    # fallback (unknown layout): any job links + the ItemList names ("Title at Company — Remote")
    names = {}
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "ItemList":
            for it in d.get("itemListElement") or []:
                names[it.get("url")] = it.get("name") or ""
    anchors = {}
    for a in soup.find_all("a", href=True):
        if JOB_URL_RE.match(a["href"]) and _text(a) and _text(a).lower() not in ("view job", "apply now"):
            anchors.setdefault(a["href"].split("?")[0], _text(a))
    for url in dict.fromkeys(m.group(0) for m in JOB_URL_RE.finditer(html)):
        name = (names.get(url) or anchors.get(url)
                or url.rsplit("/", 1)[-1].split("-", 1)[-1].replace("-", " "))
        title, _, company = name.partition(" at ")
        jobs.append(Job(source="html_boards", site="jaabz", title=title.strip(), url=url,
                        company=company.split("—")[0].strip() or None,
                        is_remote=True if "remote" in name.lower() else None, target="Jaabz"))
    return jobs


def jaabz_ai_md_url(job_url: str) -> str | None:
    m = JOB_URL_RE.match(job_url)
    return f"https://jaabz.com/jobs/{m.group(1)}/ai.md" if m else None


def parse_jaabz_detail(html: str) -> dict:
    jp = jsonld_jobposting(html)
    out = _jsonld_fields(jp) if jp else {}
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    text = " ".join(soup.stripped_strings)
    i = text.find("Job Description")
    if i >= 0:
        out["description"] = text[i + len("Job Description"):].strip()[:12000]
    return out


# ---------------- generic collect ----------------
BOARDS: dict[str, dict] = {
    "kariyer": {"parse_list": parse_kariyer_list, "detail": "kariyer"},
    "jaabz": {"parse_list": parse_jaabz_list, "detail": "jaabz"},
}


def _fetch_detail(session, board: str, job: Job) -> dict:
    if board == "jaabz":
        md = jaabz_ai_md_url(job.url)
        if md:
            try:
                txt = _get(session, md)
                if len(txt.strip()) > 200 and "<html" not in txt[:500].lower():
                    return {"description": txt.strip()[:12000]}
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001 - fall back to the HTML page
                log.debug("jaabz ai.md failed for %s: %s", job.url, e)
        return parse_jaabz_detail(_get(session, job.url))
    return parse_kariyer_detail(_get(session, job.url))


def collect(cfg: dict, want_details: Callable[[Job], bool] | None = None,
            session: requests.Session | None = None, sleep_fn=time.sleep) -> CollectResult:
    result = CollectResult()
    sources = cfg.get("extra_sources") or {}
    for board, spec in BOARDS.items():
        bcfg = sources.get(board) or {}
        if not bcfg.get("enabled", False):
            continue
        sess = session or make_session(bcfg)
        jobs: list[Job] = []
        blocked = False
        for i, url in enumerate(bcfg.get("list_urls") or []):
            if i:
                sleep_fn(random.uniform(2, 5))
            try:
                found = spec["parse_list"](_get(sess, url))
            except Blocked as e:
                result.call(board, error=f"{url}: {e}")
                log.warning("%s blocked on %s: skipping it for this run", board, url)
                blocked = True
                break
            except Exception as e:  # noqa: BLE001
                result.call(board, error=f"{url}: {type(e).__name__}: {e}")
                log.warning("%s list %s failed: %s", board, url, e)
                continue
            fresh = [j for j in found if j.url not in {x.url for x in jobs}]
            jobs.extend(fresh)
            result.call(board, found=len(fresh))
            log.info("%s | %s -> %d jobs", board, url, len(fresh))

        # details: new + relevant + not clearly on-site, slowly
        todo = [j for j in jobs if j.is_remote is not False and (want_details is None or want_details(j))]
        todo = todo[: int(bcfg.get("max_details", 20))]
        lo, hi = bcfg.get("detail_delay_sec", [3, 7])
        misses = 0
        for k, job in enumerate(todo):
            if blocked:
                break
            if k:
                sleep_fn(random.uniform(lo, hi))
            try:
                d = _fetch_detail(sess, board, job)
            except Blocked as e:
                result.call(board, error=f"job pages: {e}")
                break
            except Exception as e:  # noqa: BLE001
                d = {}
                log.warning("%s detail %s failed: %s", board, job.url, e)
            if d.get("description"):
                job.description = d["description"]
                misses = 0
            else:
                misses += 1
                if misses >= 3:
                    result.call(board, error="job pages returned nothing 3x; stopped details")
                    break
            for key in ("company", "date_posted"):
                if d.get(key) and not getattr(job, key):
                    setattr(job, key, d[key])
            if d.get("is_remote") and job.is_remote is None:
                job.is_remote = True
        if todo:
            log.info("%s: fetched %d job pages", board, len(todo))
        result.jobs.extend(jobs)
    return result
