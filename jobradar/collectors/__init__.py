from __future__ import annotations

from . import arbeitnow, jobspy_collector
from .base import CollectResult


def collect_all(cfg: dict, proxies: list[str]) -> CollectResult:
    result = CollectResult()
    result.merge(jobspy_collector.collect(cfg, proxies))
    result.merge(arbeitnow.collect(cfg))
    return result


__all__ = ["CollectResult", "collect_all"]
