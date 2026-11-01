from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..models import Job


@dataclass
class SourceHealth:
    calls: int = 0
    found: int = 0
    errors: int = 0
    last_error: str | None = None


@dataclass
class CollectResult:
    jobs: list[Job] = field(default_factory=list)
    # health per source key ("linkedin", "indeed", "arbeitnow", ...)
    health: dict[str, SourceHealth] = field(default_factory=lambda: defaultdict(SourceHealth))

    def call(self, source: str, found: int = 0, error: str | None = None) -> None:
        h = self.health[source]
        h.calls += 1
        h.found += found
        if error:
            h.errors += 1
            h.last_error = error[:500]

    def merge(self, other: "CollectResult") -> None:
        self.jobs.extend(other.jobs)
        for k, h in other.health.items():
            mine = self.health[k]
            mine.calls += h.calls
            mine.found += h.found
            mine.errors += h.errors
            mine.last_error = h.last_error or mine.last_error
