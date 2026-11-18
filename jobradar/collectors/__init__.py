from __future__ import annotations

from . import arbeitnow, feeds, html_boards, jobspy_collector, remote_boards
from .base import CollectResult


def _call_collector(fn, attempts: list[tuple[tuple, dict]]):
    last_type_error = None
    for args, kwargs in attempts:
        try:
            return fn(*args, **kwargs)
        except TypeError as e:
            last_type_error = e
            continue
    if last_type_error is not None:
        raise last_type_error
    return fn()


def collect_all(cfg: dict, proxies: list[str], want_details=None, db=None) -> CollectResult:
    result = CollectResult()
    collectors = [
        (jobspy_collector, [
            ((cfg, proxies), {"want_details": want_details, "db": db}),
            ((cfg, proxies), {"want_details": want_details}),
        ]),
        (arbeitnow, [
            ((cfg,), {"db": db}),
            ((cfg,), {}),
        ]),
        (remote_boards, [
            ((cfg,), {"db": db}),
            ((cfg,), {}),
        ]),
        (html_boards, [
            ((cfg,), {"want_details": want_details, "db": db}),
            ((cfg,), {"want_details": want_details}),
        ]),
        (feeds, [
            ((cfg,), {"db": db}),
            ((cfg,), {}),
        ]),
    ]
    for mod, attempts in collectors:
        try:
            result.merge(_call_collector(mod.collect, attempts))
        except Exception:  # noqa: BLE001 - one collector crashing must not stop others
            import logging
            logging.getLogger(__name__).exception("collector %s failed", getattr(mod, "__name__", "unknown"))
    return result


__all__ = ["CollectResult", "collect_all"]
