from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "mkt_tok", "ref", "refid",
    "ref_id", "source", "src", "trk", "trackingid", "tracking_id", "utm_campaign",
    "utm_content", "utm_medium", "utm_source", "utm_term",
}


def _clean_url(url: str) -> str:
    # drop tracking query strings so the same posting isn't stored twice
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip("/")
    path = parts.path or ""
    host = (parts.hostname or "").lower()
    if host.endswith("linkedin.com") and "/jobs/view/" in path:
        # LinkedIn view URLs are stable by path; query params are tracking/session.
        return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path.rstrip("/"), "", ""))
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _TRACKING_PARAMS:
            continue
        kept.append((key, value))
    query = urlencode(kept, doseq=True)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", path).rstrip("/")
    fragment = "" if not parts.fragment or parts.fragment.lower() in {"", "apply", "job", "content"} else parts.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


@dataclass
class Job:
    source: str  # collector name, e.g. "jobspy", "arbeitnow"
    site: str  # board, e.g. "linkedin", "indeed", "bayt"
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    description: str | None = None
    is_remote: bool | None = None
    date_posted: date | None = None
    salary: str | None = None
    target: str | None = None  # which config target found it

    # filled by the pipeline
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.url = _clean_url(self.url)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        """Same job reposted on another board -> same fingerprint."""
        key = f"{_norm(self.company)}|{_norm(self.title)}|{_norm((self.location or '').split(',')[0])}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.company or ''}\n{self.location or ''}\n{self.description or ''}"
