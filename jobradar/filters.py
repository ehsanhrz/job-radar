"""Hard filters (drop) + scoring (rank). All rules live in config.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .models import Job

TURKISH_MARKERS = re.compile(
    r"\b(ve|için|ile|deneyim|deneyimli|yazılım|geliştirici|geliştirme|aranıyor|"
    r"tercihen|bilgisi|sahip|olan|uzmanı|mühendisi|çalışma|ekibimize|arkadaş)\b",
    re.IGNORECASE,
)


def _compile(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in (patterns or [])]


def _compile_weights(d: dict[str, int] | None) -> list[tuple[re.Pattern, int, str]]:
    return [(re.compile(p, re.IGNORECASE), int(w), p) for p, w in (d or {}).items()]


def looks_turkish(text: str) -> bool:
    return len(TURKISH_MARKERS.findall(text or "")) >= 4


@dataclass
class Verdict:
    status: str  # filtered | low | queued
    score: int
    reasons: list[str]
    filter_reason: str | None = None


class Evaluator:
    def __init__(self, cfg: dict):
        f = cfg.get("filters") or {}
        s = cfg.get("scoring") or {}
        self.title_include = _compile(f.get("title_include"))
        self.title_exclude = _compile(f.get("title_exclude"))
        self.hard_exclude = _compile(f.get("hard_exclude"))
        self.location_exclude = _compile(f.get("location_exclude"))
        self.max_age_days = f.get("max_age_days")

        self.title_kw = _compile_weights(s.get("title_keywords"))
        self.text_kw = _compile_weights(s.get("text_keywords"))
        self.penalties = _compile_weights(s.get("penalties"))
        self.remote_bonus = int(s.get("remote_bonus", 0))
        self.turkish_bonus = int(s.get("turkish_bonus", 0))
        self.fresh_bonus = int(s.get("fresh_bonus", 0))
        self.target_bonus = {k: int(v) for k, v in (s.get("target_bonus") or {}).items()}
        self.min_score = int(s.get("min_score", 30))

    def title_ok(self, job: Job) -> bool:
        """Cheap pre-check (title rules only) used before fetching a description."""
        title = job.title or ""
        if self.title_include and not any(p.search(title) for p in self.title_include):
            return False
        return not any(p.search(title) for p in self.title_exclude)

    def evaluate(self, job: Job, today: date | None = None) -> Verdict:
        today = today or date.today()
        title = job.title or ""
        text = job.text

        # ---- hard filters ----
        if self.title_include and not any(p.search(title) for p in self.title_include):
            return Verdict("filtered", 0, [], "title: no include match")
        for p in self.title_exclude:
            if p.search(title):
                return Verdict("filtered", 0, [], f"title_exclude: {p.pattern}")
        for p in self.location_exclude:
            if p.search(job.location or ""):
                return Verdict("filtered", 0, [], f"location: {(job.location or '')[:60]}")
        for p in self.hard_exclude:
            m = p.search(text)
            if m:
                return Verdict("filtered", 0, [], f"hard_exclude: {m.group(0)[:60]}")
        if self.max_age_days and job.date_posted:
            if (today - job.date_posted).days > int(self.max_age_days):
                return Verdict("filtered", 0, [], "too old")

        # ---- scoring ----
        score, reasons = 0, []
        for p, w, raw in self.title_kw:
            m = p.search(title)
            if m:
                score += w
                reasons.append(f"title:{_label(m.group(0), raw)} {w:+d}")
        for p, w, raw in (*self.text_kw, *self.penalties):
            m = p.search(text)
            if m:
                score += w
                reasons.append(f"{_label(m.group(0), raw)} {w:+d}")
        if job.is_remote or re.search(r"\bremote\b|uzaktan|home.?office", text, re.I):
            if self.remote_bonus:
                score += self.remote_bonus
                reasons.append(f"remote {self.remote_bonus:+d}")
        if self.turkish_bonus and looks_turkish(text):
            score += self.turkish_bonus
            reasons.append(f"turkish {self.turkish_bonus:+d}")
        if self.fresh_bonus and job.date_posted and (today - job.date_posted).days <= 2:
            score += self.fresh_bonus
            reasons.append(f"fresh {self.fresh_bonus:+d}")
        if job.target and job.target in self.target_bonus:
            b = self.target_bonus[job.target]
            score += b
            reasons.append(f"{job.target} {b:+d}")

        status = "queued" if score >= self.min_score else "low"
        return Verdict(status, score, reasons)


def _label(matched: str, pattern: str) -> str:
    """Show what actually matched (readable in Telegram), fall back to the pattern."""
    lab = re.sub(r"\s+", " ", matched or "").strip().lower()
    return (lab or pattern)[:24]
