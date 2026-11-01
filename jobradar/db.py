from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    source        TEXT NOT NULL,
    site          TEXT NOT NULL,
    target        TEXT,
    title         TEXT NOT NULL,
    company       TEXT,
    location      TEXT,
    url           TEXT NOT NULL,
    is_remote     INTEGER,
    date_posted   TEXT,
    salary        TEXT,
    description   TEXT,
    score         INTEGER NOT NULL DEFAULT 0,
    reasons       TEXT,
    -- new | filtered | low | queued | sent | applied | skipped | saved
    status        TEXT NOT NULL,
    filter_reason TEXT,
    first_seen    TEXT NOT NULL,
    sent_at       TEXT,
    tg_message_id INTEGER,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS source_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT NOT NULL,
    source     TEXT NOT NULL,
    calls      INTEGER NOT NULL,
    found      INTEGER NOT NULL,
    errors     INTEGER NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_source ON source_runs(source, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('db_created_at', ?)", (now(),))
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after v0.1 to existing databases."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in (("keyword_score", "INTEGER"), ("ai_review", "TEXT"), ("ai_status", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

    # ---------- jobs ----------
    def seen(self, job: Job) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? OR fingerprint = ? LIMIT 1",
            (job.id, job.fingerprint),
        ).fetchone()
        return row is not None

    def insert(self, job: Job, status: str, filter_reason: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs (id, fingerprint, source, site, target, title, company,
               location, url, is_remote, date_posted, salary, description, score, reasons,
               status, filter_reason, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.id, job.fingerprint, job.source, job.site, job.target, job.title, job.company,
                job.location, job.url,
                None if job.is_remote is None else int(job.is_remote),
                job.date_posted.isoformat() if job.date_posted else None,
                job.salary, job.description, job.score, json.dumps(job.reasons),
                status, filter_reason, now(), now(),
            ),
        )
        self.conn.commit()

    def queued(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY score DESC, first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_sent(self, job_id: str, message_id: int | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='sent', sent_at=?, tg_message_id=?, updated_at=? WHERE id=?",
            (now(), message_id, now(), job_id),
        )
        self.conn.commit()

    def set_status(self, job_id: str, status: str) -> sqlite3.Row | None:
        self.conn.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), job_id)
        )
        self.conn.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def by_status(self, statuses: tuple[str, ...], limit: int = 30) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()

    def update_evaluation(self, job_id: str, status: str, score: int, reasons: list[str],
                          filter_reason: str | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, score=?, reasons=?, filter_reason=?, updated_at=? WHERE id=?",
            (status, score, json.dumps(reasons), filter_reason, now(), job_id),
        )
        self.conn.commit()

    def ai_candidates(self, min_low_score: int, limit: int) -> list[sqlite3.Row]:
        """Unreviewed queued jobs + borderline 'low' ones the AI might promote."""
        return self.conn.execute(
            """SELECT * FROM jobs WHERE ai_status IS NULL
               AND (status = 'queued' OR (status = 'low' AND score >= ?))
               ORDER BY score DESC LIMIT ?""",
            (min_low_score, limit),
        ).fetchall()

    def save_ai(self, job_id: str, keyword_score: int, final_score: int, review_json: str,
                status: str, filter_reason: str | None) -> None:
        self.conn.execute(
            """UPDATE jobs SET keyword_score=?, score=?, ai_review=?, ai_status='ok',
               status=?, filter_reason=?, updated_at=? WHERE id=?""",
            (keyword_score, final_score, review_json, status, filter_reason, now(), job_id),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def top_filter_reasons(self, limit: int = 8) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """SELECT filter_reason, COUNT(*) c FROM jobs WHERE status='filtered'
               GROUP BY filter_reason ORDER BY c DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [(r["filter_reason"], r["c"]) for r in rows]

    # ---------- source health ----------
    def record_source_run(self, source: str, calls: int, found: int, errors: int,
                          last_error: str | None) -> None:
        self.conn.execute(
            "INSERT INTO source_runs (run_at, source, calls, found, errors, last_error) VALUES (?,?,?,?,?,?)",
            (now(), source, calls, found, errors, last_error),
        )
        self.conn.commit()

    def last_source_runs(self, source: str, n: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_runs WHERE source=? ORDER BY id DESC LIMIT ?", (source, n)
        ).fetchall()

    def source_summary(self, last_n: int = 5) -> list[dict]:
        sources = [r["source"] for r in self.conn.execute("SELECT DISTINCT source FROM source_runs")]
        out = []
        for s in sources:
            runs = self.last_source_runs(s, last_n)
            out.append({
                "source": s,
                "runs": len(runs),
                "found": sum(r["found"] for r in runs),
                "errors": sum(r["errors"] for r in runs),
                "calls": sum(r["calls"] for r in runs),
            })
        return out

    # ---------- meta ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()
