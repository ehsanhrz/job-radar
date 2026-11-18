"""Handles button taps and a few commands. Runs either:
- `python -m jobradar bot`  -> always-on long polling (VPS / docker)
- inside `run`               -> drains taps made while offline (local morning mode)
"""
from __future__ import annotations

import html
import json
import logging
import math
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
    "/review [page] – inspect possible and low-confidence V2 matches\n"
    "/vetoed – jobs the AI dropped, with its reason\n"
    "/rescore – re-apply the current rules to unsent jobs, then send new matches\n"
    "/report [24h|7d|all] – full JSON report: runs, sources, issues, jobs\n"
    "/companies – company hiring boards the bot found and watches\n"
    "/run – scrape now (asks for <code>/run force</code> if a run finished in the last 30 min)\n"
)

_run_lock = threading.Lock()
RUN_COOLDOWN_MIN = 30
APPLIED_PAGE_SIZE = 10
REVIEW_PAGE_SIZE = 10
TELEGRAM_SAFE_LENGTH = 3900
TG_LIMIT = TELEGRAM_SAFE_LENGTH  # Telegram rejects messages over 4096 characters


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


def _companies_text(db: DB) -> str:
    from .collectors.ats import summary
    sm = summary(db)
    if not sm["total"]:
        return "No company boards discovered yet. They appear as jobs with Greenhouse/Lever/Ashby/Workable links come in."
    off = ", ".join(f"{n} {e(r)}" for r, n in sm["disabled"].items()) or "none"
    lines = [f"🏢 <b>Company boards</b>: watching {sm['watched']} of {sm['total']} (skipped: {off})"]
    for c in sm["top"][:20]:
        seen = "not checked yet" if c["last_checked"] is None else f"{c['last_kept'] or 0}/{c['last_found'] or 0} relevant"
        lines.append(f"• {e(c['company'] or c['slug'])} <i>({c['ats']})</i>: {seen}")
    return "\n".join(lines)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line breaks so HTML tags (one per line here) are never cut in half."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # a single monster line: hard cut (rare)
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def send_long(tg: Telegram, text: str) -> None:
    for chunk in split_message(text):
        tg.send(chunk)


def _vetoed_text(db: DB) -> str:
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


def is_authorized(upd: dict, settings: Settings) -> bool:
    """Only YOUR chat, and only YOU in it. Everything else is dropped silently:
    no reply, no button answer, nothing logged but the update id."""
    cq = upd.get("callback_query")
    if cq:
        chat, sender = (cq.get("message") or {}).get("chat") or {}, cq.get("from") or {}
    elif upd.get("message"):
        chat, sender = upd["message"].get("chat") or {}, upd["message"].get("from") or {}
    else:
        return False  # edited messages, channel posts, joins, inline queries...: never handled
    allowed_chat = str(settings.telegram_chat_id)
    if not allowed_chat or str(chat.get("id")) != allowed_chat:
        return False
    sender_id = str(sender.get("id"))
    allowed_users = settings.allowed_user_ids or frozenset({allowed_chat})
    # private chat: chat id == your user id. In a group, only TELEGRAM_ALLOWED_USER_IDS may act.
    return sender_id in allowed_users


def _applied_page(db: DB, page: int) -> tuple[list[str], list[list[dict]] | None]:
    total = db.count_by_status("applied")
    pages = max(1, math.ceil(total / APPLIED_PAGE_SIZE))
    if page < 1 or page > pages:
        return [f"Applied jobs: {total}\nInvalid page {page}. Choose a page from 1 to {pages}."], None
    rows = db.page_by_status("applied", APPLIED_PAGE_SIZE, (page - 1) * APPLIED_PAGE_SIZE)
    start = (page - 1) * APPLIED_PAGE_SIZE + 1 if total else 0
    end = min(page * APPLIED_PAGE_SIZE, total)
    header = f"<b>Applied jobs: {total}</b>\nShowing {start}–{end} of {total}\nPage {page} of {pages}"
    if not rows:
        return [header + "\n\nNo applied jobs yet."], None
    blocks = [header]
    for row in rows:
        date_info = f"\n📅 Posted: {e(row['date_posted'])}" if row["date_posted"] else ""
        applied_info = f"\n🕒 Updated: {e(row['updated_at'])}" if row["updated_at"] else ""
        blocks.append(
            f'\n\n<b>{e(row["title"])}</b>\n🏢 {e(row["company"] or "—")}\n'
            f'📍 {e(row["location"] or "—")}{date_info}{applied_info}\n'
            f'<a href="{html.escape(row["url"], quote=True)}">Open job</a>'
        )
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(current) + len(block) > TELEGRAM_SAFE_LENGTH and current:
            messages.append(current)
            current = block.lstrip()
        else:
            current += block
    if current:
        messages.append(current)
    buttons = None
    if pages > 1:
        nav = []
        if page > 1:
            nav.append({"text": "‹ Previous", "callback_data": f"ap:{page - 1}"})
        if page < pages:
            nav.append({"text": "Next ›", "callback_data": f"ap:{page + 1}"})
        buttons = [nav]
    return messages, buttons


def _review_page(db: DB, page: int) -> tuple[str, list[list[dict]] | None]:
    total = db.review_candidates(1, 0)[0]
    pages = max(1, math.ceil(total / REVIEW_PAGE_SIZE))
    if page < 1 or page > pages:
        return f"V2 review queue: {total}\nInvalid page {page}. Choose 1–{pages}.", None
    _, rows = db.review_candidates(REVIEW_PAGE_SIZE, (page - 1) * REVIEW_PAGE_SIZE)
    start = (page - 1) * REVIEW_PAGE_SIZE + 1 if total else 0
    end = min(page * REVIEW_PAGE_SIZE, total)
    lines = [f"<b>V2 review queue: {total}</b>", f"Showing {start}–{end} · page {page} of {pages}"]
    if not rows:
        lines.append("No possible or low-confidence matches.")
    for row in rows:
        lines.append(f"\n<b>{e(row['technical_fit_score'])}/100</b> · {e(row['role_family'])} · "
                     f"{e(row['seniority'])} · {float(row['confidence'] or 0):.0%}\n"
                     f"{e((row['title'] or '')[:120])} — {e((row['company'] or '—')[:70])}\n"
                     f"Explain: <code>{e(row['id'])}</code>")
    nav = []
    if page > 1:
        nav.append({"text": "‹ Previous", "callback_data": f"rv:{page - 1}"})
    if page < pages:
        nav.append({"text": "Next ›", "callback_data": f"rv:{page + 1}"})
    return "\n".join(lines), [nav] if nav else None


def _rescore(db: DB, tg: Telegram, settings: Settings, coordinator=None) -> None:
    """Rescore unsent jobs and send new matches, never overlapping a running scrape."""
    def work() -> None:
        from .pipeline import rescore_all, send_queued
        out = rescore_all(db, settings.raw)
        sent = send_queued(db, tg, settings.raw, dry_run=False)
        tg.send(f"♻️ Rescored: {out['changed']} changed · sent {sent} · "
                f"still queued {out['now_queued'] - sent}")

    if coordinator is not None:
        # service mode: share the coordinator's lock so a scheduled run cannot
        # send the same queued job at the same time.
        if not coordinator.exclusive(work):
            tg.send("A run is in progress. Send /rescore again when it's done.")
        return
    if not _run_lock.acquire(blocking=False):
        tg.send("A run is in progress. Send /rescore again when it's done.")
        return
    try:
        work()
    finally:
        _run_lock.release()


def handle_update(db: DB, tg: Telegram, settings: Settings, upd: dict, allow_run: bool = False,
                  coordinator=None) -> None:
    if not is_authorized(upd, settings):
        log.debug("ignored update %s (not from the owner's chat)", upd.get("update_id"))
        return

    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data") or ""
        if data.startswith("rv:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                tg.answer_callback(cq["id"], "Invalid page")
                return
            text, buttons = _review_page(db, page)
            tg.send(text, buttons)
            tg.answer_callback(cq["id"])
        elif data.startswith("ap:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                tg.answer_callback(cq["id"], "Invalid page")
                return
            texts, buttons = _applied_page(db, page)
            for text in texts:
                tg.send(text, buttons if text == texts[-1] else None)
            tg.answer_callback(cq["id"])
        elif data.startswith("st:"):
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
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):  # "/report@my_bot 7d" -> "/report 7d"
        head, _, rest = text.partition(" ")
        text = (head.split("@")[0] + (" " + rest if rest else "")).strip()
    if text in ("/start", "/help"):
        tg.send(HELP)
    elif text == "/stats":
        tg.send(_stats_text(db))
    elif text == "/saved":
        tg.send(_list_text(db, "saved"))
    elif text == "/applied" or text.startswith("/applied "):
        arg = text.partition(" ")[2].strip()
        try:
            page = int(arg) if arg else 1
        except ValueError:
            page = 0
        texts, buttons = _applied_page(db, page)
        for i, body in enumerate(texts):
            tg.send(body, buttons if i == len(texts) - 1 else None)
    elif text == "/review" or text.startswith("/review "):
        arg = text.partition(" ")[2].strip()
        try:
            page = int(arg) if arg else 1
        except ValueError:
            page = 0
        body, buttons = _review_page(db, page)
        tg.send(body, buttons)
    elif text == "/report" or text.startswith("/report "):
        from .report import caption, report_json
        arg = text.split(maxsplit=1)[1] if " " in text else "24h"
        try:
            name, data = report_json(db, settings.raw, arg)
            tg.send_document(name, data, caption(json.loads(data)))
        except ValueError as err:
            tg.send(f"⚠️ {e(str(err))}")
        except TelegramError as err:
            log.error("report send failed: %s", err)
            tg.send("⚠️ Couldn't send the report file, check the logs.")
    elif text == "/companies":
        send_long(tg, _companies_text(db))
    elif text == "/vetoed":
        send_long(tg, _vetoed_text(db))
    elif text == "/rescore":
        _rescore(db, tg, settings, coordinator)
    elif text in ("/run", "/run force"):
        if coordinator is not None:
            try:
                started = coordinator.start_manual("telegram")
            except TypeError:
                started = coordinator.start_manual()
            tg.send("🛰 Starting a run…" if started else "A run is already in progress.")
        elif not allow_run:
            tg.send("/run only works when the bot is running in always-on mode.")
        else:
            # cooldown counts from the last run that actually FINISHED, so a run
            # killed by a redeploy doesn't block /run
            last = db.get_meta("last_run_finished_at")
            mins = None
            if last:
                mins = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
            if text == "/run" and mins is not None and mins < RUN_COOLDOWN_MIN:
                tg.send(f"Last run finished {mins:.0f} min ago. Send <code>/run force</code> to run anyway.")
            elif try_start_run(settings, background=True):
                tg.send("🛰 Starting a run…")
            else:
                tg.send("A run is already in progress.")


def try_start_run(settings: Settings, notify_busy: bool = True, background: bool = True,
                  trigger: str = "manual") -> bool:
    """Start a scrape unless one is running. Shared by /run and the scheduler."""
    if not _run_lock.acquire(blocking=False):
        return False

    def _go() -> None:
        from .pipeline import run_once
        try:
            # the bot thread owns getUpdates, so the run must not poll Telegram
            run_once(settings, process_updates=False, trigger=trigger)
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


def run_bot(settings: Settings, coordinator=None, stop_event: threading.Event | None = None,
            ready_event: threading.Event | None = None) -> None:
    db = DB(settings.db_path)
    tg = Telegram(settings.telegram_token, settings.telegram_chat_id, settings.telegram_proxy)
    # don't crash-loop the container if Telegram is unreachable at boot
    while stop_event is None or not stop_event.is_set():
        try:
            log.info("bot online as @%s", tg.me().get("username"))
            break
        except TelegramError as err:
            log.warning("Telegram not reachable yet: %s (retrying in 30s)", err)
            if stop_event:
                stop_event.wait(30)
            else:
                time.sleep(30)
    if ready_event:
        ready_event.set()
    while stop_event is None or not stop_event.is_set():
        offset = db.get_meta("tg_offset")
        try:
            updates = tg.get_updates(int(offset) if offset else None, timeout=50)
        except TelegramError as err:
            log.warning("polling error: %s (retrying in 10s)", err)
            if stop_event:
                stop_event.wait(10)
            else:
                time.sleep(10)
            continue
        for upd in updates:
            try:
                handle_update(db, tg, settings, upd, allow_run=coordinator is None,
                              coordinator=coordinator)
            except Exception:  # noqa: BLE001
                log.exception("update failed")
            db.set_meta("tg_offset", str(upd["update_id"] + 1))
