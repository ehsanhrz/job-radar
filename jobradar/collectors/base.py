from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..models import Job


@dataclass
class SourceHealth:
    calls: int = 0
    found: int = 0
    errors: int = 0
    last_error: str | None = None
    skipped: int = 0
    duration_sec: float | None = None
    raw_found: int | None = None
    unique_found: int | None = None
    cooldown_until: str | None = None
    details: dict | None = None

    def note(self, **fields) -> None:
        for key, value in fields.items():
            if value is None:
                continue
            if key == "details":
                cur = self.details or {}
                if isinstance(value, dict):
                    cur.update(value)
                self.details = cur
            elif hasattr(self, key):
                setattr(self, key, value)


@dataclass
class CollectResult:
    jobs: list[Job] = field(default_factory=list)
    # health per source key ("linkedin", "indeed", "arbeitnow", ...)
    health: dict[str, SourceHealth] = field(default_factory=lambda: defaultdict(SourceHealth))

    def call(self, source: str, found: int = 0, error: str | None = None, **metrics) -> None:
        h = self.health[source]
        h.calls += 1
        h.found += found
        if error:
            h.errors += 1
            h.last_error = error[:500]
        if metrics:
            h.note(**metrics)

    def skip(self, source: str, reason: str, cooldown_until: str | None = None, **metrics) -> None:
        h = self.health[source]
        h.skipped += 1
        details = dict(metrics.pop("details", {}) or {})
        details.setdefault("skip_reason", reason)
        h.note(cooldown_until=cooldown_until, details=details, **metrics)

    def merge(self, other: "CollectResult") -> None:
        self.jobs.extend(other.jobs)
        for k, h in other.health.items():
            mine = self.health[k]
            mine.calls += h.calls
            mine.found += h.found
            mine.errors += h.errors
            mine.skipped += h.skipped
            mine.last_error = h.last_error or mine.last_error
            for attr in ("duration_sec", "cooldown_until"):
                val = getattr(h, attr)
                if val is not None:
                    setattr(mine, attr, val)
            for attr in ("raw_found", "unique_found"):
                val = getattr(h, attr)
                if val is not None:
                    setattr(mine, attr, (getattr(mine, attr) or 0) + val)
            if h.details:
                merged = dict(mine.details or {})
                merged.update(h.details)
                mine.details = merged


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def in_cooldown(state: dict | None, now: datetime | None = None) -> bool:
    until = parse_iso((state or {}).get("cooldown_until"))
    return bool(until and until > (now or utcnow()))


def cooldown_active_until(state: dict | None, now: datetime | None = None) -> str | None:
    until = parse_iso((state or {}).get("cooldown_until"))
    if until and until > (now or utcnow()):
        return until.isoformat(timespec="seconds")
    return None


def should_skip_fetch(state: dict | None, min_interval_hours: float | None, now: datetime | None = None,
                      cooldown_ok: bool = True) -> tuple[bool, str | None]:
    now = now or utcnow()
    if cooldown_ok and in_cooldown(state, now):
        return True, "cooldown"
    if min_interval_hours and min_interval_hours > 0:
        last = parse_iso((state or {}).get("last_success_at")) or parse_iso((state or {}).get("last_attempt_at"))
        if last and (now - last) < timedelta(hours=float(min_interval_hours)):
            return True, "min_interval"
    return False, None


def save_state(db, source: str, **fields) -> None:
    if db is None:
        return
    try:
        db.save_source_state(source, **fields)
    except Exception:  # noqa: BLE001 - state persistence must never break collection
        pass


def get_state(db, source: str) -> dict | None:
    if db is None:
        return None
    try:
        return db.get_source_state(source)
    except Exception:  # noqa: BLE001
        return None


def record_attempt(db, source: str) -> None:
    save_state(db, source, last_attempt_at=iso_now())


def record_success(db, source: str, **fields) -> None:
    payload = {
        "last_success_at": iso_now(),
        "consecutive_failures": 0,
        "last_error": None,
        "cooldown_until": None,
    }
    payload.update(fields)
    save_state(db, source, **payload)


def record_failure(db, source: str, error: str | None, cooldown_minutes: int | None = None, **fields) -> None:
    state = get_state(db, source) or {}
    failures = int(state.get("consecutive_failures") or 0) + 1
    payload = dict(fields)
    payload["consecutive_failures"] = failures
    payload["last_error"] = (error or "")[:500] or None
    if cooldown_minutes and cooldown_minutes > 0:
        payload["cooldown_until"] = (utcnow() + timedelta(minutes=cooldown_minutes)).isoformat(timespec="seconds")
    save_state(db, source, **payload)


def dumps_details(details) -> str | None:
    if details is None:
        return None
    if isinstance(details, str):
        return details
    try:
        return json.dumps(details, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(details)
