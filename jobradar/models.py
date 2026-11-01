from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _clean_url(url: str) -> str:
    # drop tracking query strings so the same posting isn't stored twice
    url = (url or "").strip()
    if "linkedin.com/jobs/view" in url:
        url = url.split("?")[0]
    return url.rstrip("/")


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
