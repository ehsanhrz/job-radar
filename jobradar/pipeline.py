from __future__ import annotations

import logging
import time

from .ai import AIReviewer, apply_review
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


def ai_review_step(db: DB, reviewer: AIReviewer, cfg: dict) -> dict[str, int]:
    """Second opinion on jobs that passed the keyword filter. Never raises."""
    out = {"reviewed": 0, "vetoed": 0, "promoted": 0}
    if not reviewer.enabled:
        why = reviewer.why_disabled()
        if why and why != "not configured":
            log.info("AI review off: %s", why)
        return out
    acfg = cfg.get("ai") or {}
    min_score = int((cfg.get("scoring") or {}).get("min_score", 30))
    margin = int(acfg.get("review_margin", 10))
    for row in db.ai_candidates(min_score - margin, int(acfg.get("max_reviews_per_run", 30))):
        if not reviewer.enabled:  # circuit breaker tripped mid-run
            break
        try:
            review = reviewer.review(dict(row))
            if review is None:
                continue  # keeps its keyword score; may be retried next run if still unsent
            final, veto = apply_review(row["score"], review, cfg)
            status = "filtered" if veto else ("queued" if final >= min_score else "low")
            db.save_ai(row["id"], row["score"], final, review.to_json(), status, veto)
            out["reviewed"] += 1
            out["vetoed"] += bool(veto)
            out["promoted"] += row["status"] == "low" and status == "queued"
        except Exception:  # noqa: BLE001 - belt and braces: AI must never break a run
            log.exception("AI step error on %s; keeping keyword score", row["id"])
    return out


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
        db.set_meta("next_run_at", None)    # a one-off resume slot is consumed by any run
    tg = None if dry_run else Telegram(settings.telegram_token, settings.telegram_chat_id,
                                       settings.telegram_proxy)

    if tg and process_updates:
        # handle Applied/Skip/Save taps made while we were offline
        from .bot import process_pending
        process_pending(db, tg, settings)

    started = time.time()
    evaluator = Evaluator(cfg)
    result = collect_all(cfg, settings.scrape_proxies,
                         want_details=lambda job: not db.seen(job) and evaluator.title_ok(job))
    record_health(db, result)
    counts = ingest(db, evaluator, result)
    reviewer = AIReviewer(cfg)
    ai = ai_review_step(db, reviewer, cfg)
    sent = send_queued(db, tg, cfg, dry_run)
    alerts = watchdog(db, result, cfg, tg)

    summary = {
        **counts,
        "sent": sent,
        "still_queued": len(db.queued(10_000)),
        "minutes": round((time.time() - started) / 60, 1),
        "ai": {**ai, **reviewer.stats, "off": reviewer.why_disabled()},
        "sources": {k: f"{h.found} jobs / {h.calls} calls / {h.errors} err" for k, h in result.health.items()},
    }
    log.info("run summary: %s", summary)
    if not dry_run:
        from .db import now
        db.set_meta("last_run_finished_at", now())  # absent/older = the run was killed

    if tg and (cfg.get("telegram") or {}).get("send_run_summary", True):
        src = "\n".join(f"• {k}: {v}" for k, v in summary["sources"].items()) or "• (no sources ran)"
        try:
            tg.send(
                f"🛰 <b>Run done</b> in {summary['minutes']} min\n"
                f"new {counts['new']} · sent {sent} · low-score {counts['low']} · "
                f"filtered {counts['filtered']} · dupes {counts['duplicate']}\n"
                f"{_ai_summary(summary['ai'])}{src}"
            )
        except TelegramError as e:
            log.error("summary failed: %s", e)
    summary["alerts"] = len(alerts)
    return summary


def _ai_summary(ai: dict) -> str:
    off = ai.get("off")
    if off == "not configured":
        return ""  # AI not set up: say nothing
    line = (f"🤖 AI reviewed {ai['reviewed']} · vetoed {ai['vetoed']} · promoted {ai['promoted']}"
            f" · failed {ai['failed']}")
    if off:
        line += f" · ⚠️ {off}, used keyword scores"
    return line + "\n"
