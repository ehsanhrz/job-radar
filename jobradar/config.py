from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class Settings:
    raw: dict[str, Any]
    telegram_token: str
    telegram_chat_id: str
    telegram_proxy: str | None
    scrape_proxies: list[str]
    db_path: Path

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}


def load_settings(config_path: str | None = None) -> Settings:
    load_dotenv()
    path = Path(config_path or os.getenv("JOBRADAR_CONFIG", "config.yaml"))
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    proxies = [p.strip() for p in os.getenv("SCRAPE_PROXIES", "").split(",") if p.strip()]
    db_path = Path(os.getenv("JOBRADAR_DB", "data/jobradar.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        raw=raw,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip() or None,
        scrape_proxies=proxies,
        db_path=db_path,
    )
