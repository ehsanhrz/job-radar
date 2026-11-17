from __future__ import annotations

import logging
import json
import time

from .ai import AIReviewer, Review, apply_review, job_text
from .collectors import CollectResult, collect_all
from .config import Settings
from .db import DB
from .filters import Evaluator
from .matching import EVALUATION_VERSION, MatchingEngine, TechnicalEvaluation
from .telegram import Telegram, TelegramError, format_job, job_buttons

log = logging.getLogger(__name__)


def ingest(db: DB, evaluator: Evaluator, result: CollectResult) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0, "filtered": 0, "low": 0, "queued": 0}
    score_distribution = {"0-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    for job in result.jobs:
        if db.seen(job):
            counts["duplicate"] += 1
            continue
        try:
            v = evaluator.evaluate(job)
        except Exception:  # noqa: BLE001 - one malformed description must not abort collection
            log.exception("evaluation failed for job %s", job.id)
            counts.setdefault("evaluation_errors", 0)
            counts["evaluation_errors"] += 1
            continue
        if isinstance(v, TechnicalEvaluation):
            status, filter_reason = evaluator.status(v)
            job.score, job.reasons = v.technical_fit_score, v.reasons
            db.insert(job, status, filter_reason, evaluation=v.to_dict())
            bucket = "0-39" if v.technical_fit_score < 40 else "40-59" if v.technical_fit_score < 60 else \
                     "60-79" if v.technical_fit_score < 80 else "80-100"
            score_distribution[bucket] += 1
        else:
            status, filter_reason = v.status, v.filter_reason
            job.score, job.reasons = v.score, v.reasons
            db.insert(job, status, filter_reason)
        counts["new"] += 1
        counts[status] += 1
    if isinstance(evaluator, MatchingEngine):
        log.info("matching v%d score distribution: %s", EVALUATION_VERSION, score_distribution)
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


DEFAULT_REVIEW_MARGIN = 10


def review_margin(cfg: dict) -> int:
    """How far below min_score the AI may still review (and promote) a job.
    One definition, so the ATS pre-filter and the AI step can never disagree."""
    v = (cfg.get("ai") or {}).get("review_margin")
    return DEFAULT_REVIEW_MARGIN if v is None else int(v)


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
    margin = review_margin(cfg)
    for row in db.ai_candidates(min_score - margin, int(acfg.get("max_reviews_per_run", 30))):
        if not reviewer.enabled:  # circuit breaker tripped mid-run
            break
        try:
            review = reviewer.review(dict(row))
            if review is None:
                continue  # keeps its keyword score; may be retried next run if still unsent
            final, veto = apply_review(row["score"], review, cfg, job_text(dict(row)))
            status = "filtered" if veto else ("queued" if final >= min_score else "low")
            db.save_ai(row["id"], row["score"], final, review.to_json(), status, veto)
            out["reviewed"] += 1
            out["vetoed"] += bool(veto)
            out["promoted"] += row["status"] == "low" and status == "queued"
        except Exception:  # noqa: BLE001 - belt and braces: AI must never break a run
            log.exception("AI step error on %s; keeping keyword score", row["id"])
    return out


def rescore_all(db: DB, cfg: dict) -> dict[str, int]:
    """Explicitly reevaluate pending jobs with V2; completed history is immutable."""
    from datetime import date
    from .models import Job
    ev = MatchingEngine(cfg)
    out = {"changed": 0, "now_queued": 0}
    for row in db.by_status(("low", "queued"), limit=100_000):
        job = _row_job(row, date)
        v = ev.evaluate(job)
        status, reason = ev.status(v)
        try:
            previous = json.loads(row["evaluation_details"] or "null")
        except (TypeError, ValueError):
            previous = None
        if previous != v.to_dict() or status != row["status"]:
            db.save_v2_evaluation(row["id"], v.to_dict(), status, reason)
            out["changed"] += 1
    out["now_queued"] = len(db.queued(100_000, evaluation_version=EVALUATION_VERSION))
    return out


def _row_job(row, date_type=None):
    from datetime import date as date_cls
    from .models import Job
    date_type = date_type or date_cls
    return Job(source=row["source"], site=row["site"], title=row["title"], url=row["url"],
               company=row["company"], location=row["location"], description=row["description"],
               is_remote=bool(row["is_remote"]) if row["is_remote"] is not None else None,
               date_posted=date_type.fromisoformat(row["date_posted"]) if row["date_posted"] else None,
               target=row["target"])


def _upgrade_pending_evaluations(db: DB, evaluator: MatchingEngine) -> int:
    """Version pending legacy rows once, without touching filtered/sent/application history."""
    from datetime import date
    changed = 0
    for row in db.unversioned_pending():
        try:
            result = evaluator.evaluate(_row_job(row, date))
            status, reason = evaluator.status(result)
            changed += db.save_v2_evaluation(row["id"], result.to_dict(), status, reason)
        except Exception:  # noqa: BLE001
            log.exception("V2 migration evaluation failed for pending job %s", row["id"])
    if changed:
        log.info("versioned %d legacy pending jobs with matching v%d", changed, EVALUATION_VERSION)
    return changed


def send_queued(db: DB, tg: Telegram | None, cfg: dict, dry_run: bool,
                evaluation_version: int | None = None) -> int:
    tcfg = cfg.get("telegram") or {}
    rows = db.queued(int(tcfg.get("max_per_run", 20)), evaluation_version=evaluation_version)
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


def run_once(settings: Settings, dry_run: bool = False, process_updates: bool = True,
             trigger: str = "cli") -> dict:
    cfg = settings.raw
    db = DB(settings.db_path)
    run_id = None
    if not dry_run:
        from .db import now
        db.set_meta("last_run_at", now())  # serve mode schedules from this
        db.set_meta("next_run_at", None)    # a one-off resume slot is consumed by any run
        run_id = db.start_run(trigger)
    tg = None if dry_run else Telegram(settings.telegram_token, settings.telegram_chat_id,
                                       settings.telegram_proxy)

    if tg and process_updates:
        # handle Applied/Skip/Save taps made while we were offline
        from .bot import process_pending
        process_pending(db, tg, settings)

    started = time.time()
    evaluator = MatchingEngine(cfg)
    _upgrade_pending_evaluations(db, evaluator)
    result = collect_all(cfg, settings.scrape_proxies,
                         want_details=lambda job: not db.seen(job) and evaluator.title_ok(job))
    # company boards found in those jobs (and earlier ones), checked directly; never breaks the run
    from .collectors import ats
    # keep only postings that could be sent: queued, or low but within the AI review margin
    def ats_keep(job) -> bool:
        v = evaluator.evaluate(job)
        return evaluator.status(v)[0] in {"queued", "low"}

    result.merge(ats.run_step(db, cfg, result, keep=ats_keep))
    record_health(db, result)
    counts = ingest(db, evaluator, result)
    # Do not combine legacy AI-adjusted scores with the normalized deterministic V2 score.
    ai = {"reviewed": 0, "vetoed": 0, "promoted": 0, "failed": 0,
          "off": "disabled for matching v2"}
    sent = send_queued(db, tg, cfg, dry_run, evaluation_version=EVALUATION_VERSION)
    alerts = watchdog(db, result, cfg, tg)

    summary = {
        **counts,
        "sent": sent,
        "still_queued": len(db.queued(10_000)),
        "minutes": round((time.time() - started) / 60, 1),
        "ai": ai,
        "sources": {k: f"{h.found} jobs / {h.calls} calls / {h.errors} err" for k, h in result.health.items()},
    }
    log.info("run summary: %s", summary)
    if not dry_run:
        from .db import now
        db.set_meta("last_run_finished_at", now())  # absent/older = the run was killed
        db.finish_run(run_id, summary, {
            k: {"calls": h.calls, "found": h.found, "errors": h.errors, "last_error": h.last_error}
            for k, h in result.health.items()}, alerts)

    if tg and (cfg.get("telegram") or {}).get("send_run_summary", True):
        src = "\n".join(f"• {k}: {v}" for k, v in summary["sources"].items()) or "• (no sources ran)"
        try:
            tg.send(
                f"🛰 <b>Run done</b> in {summary['minutes']} min\n"
                f"new {counts['new']} · sent {sent} · low-score {counts['low']} · "
                f"filtered {counts['filtered']} · dupes {counts['duplicate']}\n"
                f"{src}"
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
