"""Handles button taps and a few commands. Runs either:
- `python -m jobradar bot`  -> always-on long polling (VPS / docker)
- inside `run`               -> drains taps made while offline (local morning mode)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import Settings
from .db import DB
from .telegram import Telegram, TelegramError, e, format_job, job_buttons, status_buttons

log = logging.getLogger(__name__)

HELP = (
    "<b>Job Radar</b>\n"
    "/stats – counts + source health\n"
    "/saved – jobs you starred\n"
    "/applied – jobs you applied to\n"
    "/vetoed – jobs the AI dropped, with its reason\n"
    "/rescore – re-apply the current rules to unsent jobs, then send new matches\n"
    "/run – scrape now (asks for <code>/run force</code> if a run finished in the last 30 min)\n"
)

_run_lock = threading.Lock()
RUN_COOLDOWN_MIN = 30


def _stats_text(db: DB) -> str:
    st = db.stats()
    order = ["sent", "applied", "saved", "skipped", "queued", "low", "filtered"]
    counts = " · ".join(f"{k} {st.get(k, 0)}" for k in order)
    src = "\n".join(
        f"• {s['source']}: {s['found']} jobs / {s['calls']} calls / {s['errors']} err (last {s['runs']} runs)"
        for s in db.source_summary()
    ) or "• no runs yet"
    reasons = "\n".join(f"• {e(r)}: {c}" for r, c in db.top_filter_reasons(5)) or "• —"
    created = (db.get_meta("db_created_at") or "?")[:16].replace("T", " ")
    last = (db.get_meta("last_run_finished_at") or "never")[:16].replace("T", " ")
    return (f"📊 <b>Stats</b>\n{counts}\nDB since {created} UTC · last finished run {last} UTC\n\n"
            f"<b>Sources</b>\n{src}\n\n<b>Top filter reasons</b>\n{reasons}")


def _list_text(db: DB, status: str) -> str:
    rows = db.by_status((status,), limit=25)
    if not rows:
        return f"No {status} jobs yet."
    lines = [f"<b>{status.title()} ({len(rows)})</b>"]
    for r in rows:
        lines.append(f'• <a href="{e(r["url"])}">{e(r["title"])}</a> — {e(r["company"] or "?")}')
    return "\n".join(lines)


def _vetoed_text(db: DB) -> str:
    import json
    rows = db.conn.execute(
        "SELECT * FROM jobs WHERE status='filtered' AND filter_reason LIKE 'ai:%' "
        "ORDER BY updated_at DESC LIMIT 15").fetchall()
    if not rows:
        return "The AI hasn't dropped anything."
    lines = [f"<b>AI-dropped ({len(rows)})</b>"]
    for r in rows:
        rv = json.loads(r["ai_review"] or "{}")
        why = rv.get("evidence") or rv.get("reason") or ""
        lines.append(f'• <a href="{e(r["url"])}">{e(r["title"])}</a> ({e(r["company"] or "?")})\n'
                     f'   {e(r["filter_reason"])}: <i>{e(why[:120])}</i>')
    return "\n".join(lines)


def handle_update(db: DB, tg: Telegram, settings: Settings, upd: dict, allow_run: bool = False) -> None:
    allowed = str(settings.telegram_chat_id)

    cq = upd.get("callback_query")
    if cq:
        chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat_id != allowed:
            return
        data = cq.get("data") or ""
        if data.startswith("st:"):
            _, status, job_id = data.split(":", 2)
            if db.get(job_id) is None:
                log.warning("button for unknown job %s: DB was reset or another instance sent it", job_id)
                tg.answer_callback(cq["id"], "⚠️ Job not in the database (was the DB wiped on redeploy?)")
                return
            row = db.set_status(job_id, status)
            msg = cq.get("message") or {}
            buttons = job_buttons(job_id) if status == "sent" else status_buttons(job_id, status)
            try:
                tg.edit_buttons(msg["chat"]["id"], msg["message_id"], buttons)
            except TelegramError as err:
                log.warning("edit buttons: %s", err)
            tg.answer_callback(cq["id"], "Undone" if status == "sent" else status.title())
            log.info("job %s -> %s (%s)", job_id, status, row["title"] if row else "?")
        else:
            tg.answer_callback(cq["id"])
        return

    msg = upd.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != allowed:
        return
    text = (msg.get("text") or "").strip().split("@")[0]
    if text in ("/start", "/help"):
        tg.send(HELP)
    elif text == "/stats":
        tg.send(_stats_text(db))
    elif text == "/saved":
        tg.send(_list_text(db, "saved"))
    elif text == "/applied":
        tg.send(_list_text(db, "applied"))
    elif text == "/vetoed":
        tg.send(_vetoed_text(db))
    elif text == "/rescore":
        from .pipeline import rescore_all, send_queued
        out = rescore_all(db, settings.raw)
        sent = send_queued(db, tg, settings.raw, dry_run=False)
        tg.send(f"♻️ Rescored: {out['changed']} changed · sent {sent} · still queued {out['now_queued'] - sent}")
    elif text in ("/run", "/run force"):
        # cooldown counts from the last run that actually FINISHED, so a run killed
        # by a redeploy doesn't block /run
        last = db.get_meta("last_run_finished_at")
        mins = None
        if last:
            mins = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        if not allow_run:
            tg.send("/run only works when the bot is running in always-on mode.")
        elif text == "/run" and mins is not None and mins < RUN_COOLDOWN_MIN:
            tg.send(f"Last run finished {mins:.0f} min ago. Send <code>/run force</code> to run anyway.")
        elif try_start_run(settings, background=True):
            tg.send("🛰 Starting a run…")
        else:
            tg.send("A run is already in progress.")


def try_start_run(settings: Settings, notify_busy: bool = True, background: bool = True) -> bool:
    """Start a scrape unless one is running. Shared by /run and the scheduler."""
    if not _run_lock.acquire(blocking=False):
        return False

    def _go() -> None:
        from .pipeline import run_once
        try:
            # the bot thread owns getUpdates, so the run must not poll Telegram
            run_once(settings, process_updates=False)
        except Exception:  # noqa: BLE001
            log.exception("run failed")
        finally:
            _run_lock.release()

    if background:
        threading.Thread(target=_go, daemon=True, name="run").start()
    else:
        _go()
    return True


def process_pending(db: DB, tg: Telegram, settings: Settings) -> int:
    """Non-blocking: drain whatever updates are waiting (Telegram keeps them ~24h)."""
    offset = db.get_meta("tg_offset")
    handled = 0
    try:
        updates = tg.get_updates(int(offset) if offset else None, timeout=0)
    except TelegramError as err:
        log.warning("could not fetch pending updates: %s", err)
        return 0
    for upd in updates:
        try:
            handle_update(db, tg, settings, upd)
        except Exception:  # noqa: BLE001
            log.exception("update failed")
        db.set_meta("tg_offset", str(upd["update_id"] + 1))
        handled += 1
    if handled:
        log.info("processed %d pending Telegram updates", handled)
    return handled


def run_bot(settings: Settings) -> None:
    db = DB(settings.db_path)
    tg = Telegram(settings.telegram_token, settings.telegram_chat_id, settings.telegram_proxy)
    while True:  # don't crash-loop the container if Telegram is unreachable at boot
        try:
            log.info("bot online as @%s", tg.me().get("username"))
            break
        except TelegramError as err:
            log.warning("Telegram not reachable yet: %s (retrying in 30s)", err)
            time.sleep(30)
    while True:
        offset = db.get_meta("tg_offset")
        try:
            updates = tg.get_updates(int(offset) if offset else None, timeout=50)
        except TelegramError as err:
            log.warning("polling error: %s (retrying in 10s)", err)
            time.sleep(10)
            continue
        for upd in updates:
            try:
                handle_update(db, tg, settings, upd, allow_run=True)
            except Exception:  # noqa: BLE001
                log.exception("update failed")
            db.set_meta("tg_offset", str(upd["update_id"] + 1))
