from __future__ import annotations

from . import arbeitnow, html_boards, jobspy_collector, remote_boards
from .base import CollectResult


def collect_all(cfg: dict, proxies: list[str], want_details=None) -> CollectResult:
    result = CollectResult()
    result.merge(jobspy_collector.collect(cfg, proxies, want_details=want_details))
    result.merge(arbeitnow.collect(cfg))
    result.merge(remote_boards.collect(cfg))
    result.merge(html_boards.collect(cfg, want_details=want_details))
    return result


__all__ = ["CollectResult", "collect_all"]
