from __future__ import annotations

import logging
import time

from .collectors import CollectResult, collect_all
from .config import Settings
from .db import DB
from .filters import Evaluator
from .telegram import Telegram, TelegramError, format_job, job_buttons

log = logging.getLogger(__name__)


def ingest(db: DB, evaluator: Evaluator, result: CollectResult) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0, "filtered": 0, "low": 0, "queued": 0}
    for job in result.jobs:
        if db.seen(job):
            counts["duplicate"] += 1
            continue
        v = evaluator.evaluate(job)
        job.score, job.reasons = v.score, v.reasons
        db.insert(job, v.status, v.filter_reason)
        counts["new"] += 1
        counts[v.status] += 1
    return counts


def record_health(db: DB, result: CollectResult) -> None:
    for source, h in result.health.items():
        db.record_source_run(source, h.calls, h.found, h.errors, h.last_error)


def watchdog(db: DB, result: CollectResult, cfg: dict, tg: Telegram | None) -> list[str]:
    """Alert once when a source returns nothing N runs in a row; say when it recovers."""
    n = int((cfg.get("watchdog") or {}).get("zero_runs_alert", 3))
    messages = []
    for source in result.health:
        runs = db.last_source_runs(source, n)
        key = f"alert:{source}"
        alerted = db.get_meta(key)
        dead = len(runs) == n and all(r["found"] == 0 for r in runs)
        if dead and not alerted:
            err = next((r["last_error"] for r in runs if r["last_error"]), None)
            msg = (f"⚠️ <b>{source}</b> returned 0 jobs in the last {n} runs.\n"
                   f"Probably blocked / layout changed. Last error: <code>{(err or 'none')[:300]}</code>")
            messages.append(msg)
            db.set_meta(key, "1")
        elif not dead and alerted and runs and runs[0]["found"] > 0:
            messages.append(f"✅ <b>{source}</b> is back ({runs[0]['found']} jobs this run).")
            db.set_meta(key, None)
    if tg:
        for m in messages:
            try:
                tg.send(m)
            except TelegramError as e:
                log.error("watchdog alert failed: %s", e)
    for m in messages:
        log.warning("watchdog: %s", m.replace("<b>", "").replace("</b>", ""))
    return messages


def send_queued(db: DB, tg: Telegram | None, cfg: dict, dry_run: bool) -> int:
    tcfg = cfg.get("telegram") or {}
    rows = db.queued(int(tcfg.get("max_per_run", 20)))
    sent = 0
    for row in rows:
        text = format_job(row)
        if dry_run or tg is None:
            print("\n" + "-" * 60 + "\n" + text)
            continue
        try:
            msg_id = tg.send(text, job_buttons(row["id"]))
        except TelegramError as e:
            log.error("send failed for %s: %s", row["id"], e)
            break  # likely network/VPN issue; the rest stay queued for next run
        db.mark_sent(row["id"], msg_id)
        sent += 1
        time.sleep(1.1)  # stay well under Telegram's per-chat limit
    return sent


def run_once(settings: Settings, dry_run: bool = False, process_updates: bool = True) -> dict:
    cfg = settings.raw
    db = DB(settings.db_path)
    if not dry_run:
        from .db import now
        db.set_meta("last_run_at", now())  # serve mode schedules from this
    tg = None if dry_run else Telegram(settings.telegram_token, settings.telegram_chat_id,
                                       settings.telegram_proxy)

    if tg and process_updates:
        # handle Applied/Skip/Save taps made while we were offline
        from .bot import process_pending
        process_pending(db, tg, settings)

    started = time.time()
    result = collect_all(cfg, settings.scrape_proxies)
    record_health(db, result)
    counts = ingest(db, Evaluator(cfg), result)
    sent = send_queued(db, tg, cfg, dry_run)
    alerts = watchdog(db, result, cfg, tg)

    summary = {
        **counts,
        "sent": sent,
        "still_queued": len(db.queued(10_000)),
        "minutes": round((time.time() - started) / 60, 1),
        "sources": {k: f"{h.found} jobs / {h.calls} calls / {h.errors} err" for k, h in result.health.items()},
    }
    log.info("run summary: %s", summary)

    if tg and (cfg.get("telegram") or {}).get("send_run_summary", True):
        src = "\n".join(f"• {k}: {v}" for k, v in summary["sources"].items()) or "• (no sources ran)"
        try:
            tg.send(
                f"🛰 <b>Run done</b> in {summary['minutes']} min\n"
                f"new {counts['new']} · sent {sent} · low-score {counts['low']} · "
                f"filtered {counts['filtered']} · dupes {counts['duplicate']}\n{src}"
            )
        except TelegramError as e:
            log.error("summary failed: %s", e)
    summary["alerts"] = len(alerts)
    return summary
