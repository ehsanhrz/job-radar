"""Tiny Telegram Bot API client (no framework) + message formatting."""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

STATUS_LABEL = {
    "applied": "✅ Applied",
    "skipped": "⏭ Skipped",
    "saved": "⭐ Saved",
}


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str, proxy: str | None = None):
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        self.base = f"https://api.telegram.org/bot{token}"
        self._token = token
        self.chat_id = str(chat_id)
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def _call(self, method: str, *, http_timeout: int = 30, **params: Any) -> Any:
        """params go to Telegram as JSON; http_timeout is our socket timeout."""
        for attempt in range(3):
            try:
                r = self.s.post(f"{self.base}/{method}", json=params, timeout=http_timeout)
            except requests.RequestException as e:
                if attempt == 2:
                    # requests puts the URL (with the bot token) in the message: mask it
                    raise TelegramError(f"{method}: {str(e).replace(self._token, '***')}") from None
                time.sleep(3)
                continue
            data = r.json() if r.content else {}
            if r.status_code == 429:  # flood control
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                time.sleep(int(wait) + 1)
                continue
            if not data.get("ok"):
                raise TelegramError(f"{method}: {data.get('description') or r.text[:200]}")
            return data["result"]
        raise TelegramError(f"{method}: gave up after retries")

    # ---- API ----
    def send(self, text: str, buttons: list[list[dict]] | None = None) -> int:
        params: dict[str, Any] = dict(
            chat_id=self.chat_id, text=text, parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", **params)["message_id"]

    def edit_buttons(self, chat_id: int | str, message_id: int, buttons: list[list[dict]]) -> None:
        try:
            self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                       reply_markup={"inline_keyboard": buttons})
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:  # expired callbacks (>~15 min old) can't be answered: fine
            log.debug("answerCallbackQuery: %s", e)

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        params: dict[str, Any] = dict(timeout=timeout, allowed_updates=["message", "callback_query"])
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=timeout + 15, **params)

    def me(self) -> dict:
        return self._call("getMe")


# ---------- formatting ----------
def e(s: Any) -> str:
    return html.escape(str(s or ""), quote=False)


def job_buttons(job_id: str) -> list[list[dict]]:
    return [[
        {"text": "✅ Applied", "callback_data": f"st:applied:{job_id}"},
        {"text": "⏭ Skip", "callback_data": f"st:skipped:{job_id}"},
        {"text": "⭐ Save", "callback_data": f"st:saved:{job_id}"},
    ]]


def status_buttons(job_id: str, status: str) -> list[list[dict]]:
    # after a choice: show it, plus an undo
    return [[
        {"text": STATUS_LABEL.get(status, status), "callback_data": "noop"},
        {"text": "↩️ Undo", "callback_data": f"st:sent:{job_id}"},
    ]]


def format_job(row: sqlite3.Row | dict) -> str:
    r = dict(row)
    reasons = json.loads(r.get("reasons") or "[]")
    remote = " · 🌐 Remote" if r.get("is_remote") else ""
    lines = [
        f"<b>{e(r['title'])}</b>",
        f"🏢 {e(r.get('company') or '—')}",
        f"📍 {e(r.get('location') or '—')}{remote}",
    ]
    if r.get("salary"):
        lines.append(f"💰 {e(r['salary'])}")
    lines.append(f"⭐ <b>{r.get('score', 0)}</b>  <i>{e(', '.join(reasons[:6]))}</i>")
    posted = f" · posted {e(r['date_posted'])}" if r.get("date_posted") else ""
    lines.append(f"🔎 {e(r.get('site'))} · {e(r.get('target') or '')}{posted}")
    lines.append(f'<a href="{html.escape(r["url"], quote=True)}">👉 Open job</a>')
    return "\n".join(lines)
