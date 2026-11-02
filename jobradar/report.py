"""Full JSON report: what the bot did, how it went, what broke, and the jobs.

    /report        last 24h      (Telegram, sent as a .json file)
    /report 7d     last 7 days
    /report all    everything
    python -m jobradar report 7d -o report.json
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .ai import Review
from .db import DB

SNIPPET = 400


def parse_period(arg: str | None) -> tuple[str, datetime | None]:
    arg = (arg or "24h").strip().lower()
    if arg in ("all", "*"):
        return "all", None
    m = re.fullmatch(r"(\d+)\s*([hdw])", arg)
    if not m:
        raise ValueError(f"bad period {arg!r}: use e.g. 24h, 3d, 2w, all")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return arg, datetime.now(timezone.utc) - delta


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _job(row) -> dict:
    r = dict(row)
    review = Review.from_json(r.get("ai_review"))
    desc = re.sub(r"\s+", " ", r.get("description") or "").strip()
    return {
        "id": r["id"],
        "title": r["title"],
        "company": r.get("company"),
        "location": r.get("location"),
        "remote": None if r.get("is_remote") is None else bool(r["is_remote"]),
        "site": r["site"],
        "url": r["url"],
        "status": r["status"],
        "score": r["score"],
        "keyword_score": r.get("keyword_score"),
        "reasons": json.loads(r.get("reasons") or "[]"),
        "filter_reason": r.get("filter_reason"),
        "ai": review.__dict__ if review else None,
        "date_posted": r.get("date_posted"),
        "first_seen": r.get("first_seen"),
        "sent_at": r.get("sent_at"),
        "updated_at": r.get("updated_at"),
        "salary": r.get("salary"),
        "description_snippet": desc[:SNIPPET] + ("…" if len(desc) > SNIPPET else ""),
    }


def _reason_group(reason: str | None) -> str:
    """'hard_exclude: must be based in the US' -> 'hard_exclude'; keep short ones whole."""
    if not reason:
        return "?"
    head = reason.split(":", 1)[0].strip()
    return reason if head in ("title", "too old") or len(reason) < 40 else head


def build_report(db: DB, cfg: dict, period: str | None = "24h") -> dict:
    label, since = parse_period(period)
    since_s = _iso(since) or ""
    q = db.conn.execute

    runs_rows = q("SELECT * FROM runs WHERE started_at >= ? ORDER BY id", (since_s,)).fetchall()
    latest_id = q("SELECT MAX(id) m FROM runs").fetchone()["m"]
    runs, src_agg = [], defaultdict(lambda: {"runs": 0, "calls": 0, "found": 0, "errors": 0,
                                             "zero_runs": 0, "last_error": None})
    funnel = Counter()
    ai_tot = Counter()
    ai_off = Counter()
    interrupted = 0
    for r in runs_rows:
        summ = json.loads(r["summary"] or "{}")
        sources = json.loads(r["sources"] or "{}")
        killed = r["finished_at"] is None and r["id"] != latest_id
        interrupted += killed
        minutes = None
        if r["finished_at"]:
            minutes = round((datetime.fromisoformat(r["finished_at"]) -
                             datetime.fromisoformat(r["started_at"])).total_seconds() / 60, 1)
        runs.append({
            "id": r["id"], "trigger": r["trigger"], "started_at": r["started_at"],
            "finished_at": r["finished_at"], "minutes": minutes,
            "state": "killed" if killed else ("running" if r["finished_at"] is None else "done"),
            "counts": {k: summ.get(k) for k in ("new", "duplicate", "filtered", "low", "queued", "sent")
                       if k in summ},
            "ai": summ.get("ai"), "sources": sources,
            "alerts": json.loads(r["alerts"] or "[]"),
        })
        for k in ("new", "duplicate", "filtered", "low", "queued", "sent"):
            funnel[k] += int(summ.get(k) or 0)
        for name, h in sources.items():
            a = src_agg[name]
            a["runs"] += 1
            a["calls"] += h.get("calls", 0)
            a["found"] += h.get("found", 0)
            a["errors"] += h.get("errors", 0)
            a["zero_runs"] += h.get("found", 0) == 0
            a["last_error"] = h.get("last_error") or a["last_error"]
        ai = summ.get("ai") or {}
        for k in ("reviewed", "vetoed", "promoted", "ok", "failed"):
            ai_tot[k] += int(ai.get(k) or 0)
        if ai.get("off"):
            ai_off[ai["off"]] += 1
    funnel["found"] = sum(a["found"] for a in src_agg.values())

    for name, a in src_agg.items():
        if a["found"] == 0 and a["errors"]:
            a["status"] = "blocked_or_failing"
        elif a["found"] == 0:
            a["status"] = "empty"
        elif a["errors"]:
            a["status"] = "degraded"
        else:
            a["status"] = "ok"

    def jobs(sql: str, args: tuple = (), limit: int = 200) -> list[dict]:
        return [_job(r) for r in q(sql + f" LIMIT {int(limit)}", args).fetchall()]

    period_jobs = "first_seen >= ?"
    filtered_reasons = Counter(_reason_group(r["filter_reason"]) for r in q(
        f"SELECT filter_reason FROM jobs WHERE status='filtered' AND {period_jobs}", (since_s,)))

    # Stage counts come from the jobs themselves (their status NOW), not from per-run
    # counters taken before AI review: so new = filtered + ai_vetoed + low + queued + reached_you.
    outcome = Counter()
    for r in q(f"SELECT status, filter_reason FROM jobs WHERE {period_jobs}", (since_s,)):
        st = r["status"]
        if st == "filtered":
            outcome["ai_vetoed" if (r["filter_reason"] or "").startswith("ai:") else "filtered"] += 1
        elif st in ("low", "queued"):
            outcome[st] += 1
        else:                       # sent / applied / skipped / saved
            outcome["reached_you"] += 1
    funnel = Counter({"found": funnel["found"], "duplicate": funnel["duplicate"],
                      "new": sum(outcome.values()),
                      **{k: outcome[k] for k in ("filtered", "ai_vetoed", "low", "queued", "reached_you")},
                      # every job sent in the period, including ones that were queued before it
                      "sent": q("SELECT COUNT(*) c FROM jobs WHERE sent_at >= ?", (since_s,)).fetchone()["c"]})

    report = {
        "report": {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "period": label, "since": since_s or None,
            "db_created_at": db.get_meta("db_created_at"),
            "last_run_started": db.get_meta("last_run_at"),
            "last_run_finished": db.get_meta("last_run_finished_at"),
        },
        "totals_all_time": db.stats(),
        "funnel": {**dict(funnel), "filter_reasons": dict(filtered_reasons.most_common(20))},
        "sources": dict(src_agg),
        "ai": {"model": os.getenv("AI_MODEL") or None, **dict(ai_tot),
               "off_reasons": dict(ai_off)},
        "runs": runs,
        "jobs": {
            "sent_in_period": jobs("SELECT * FROM jobs WHERE sent_at >= ? ORDER BY sent_at DESC", (since_s,)),
            "queued_now": jobs("SELECT * FROM jobs WHERE status='queued' ORDER BY score DESC"),
            "applied_all_time": jobs("SELECT * FROM jobs WHERE status='applied' ORDER BY updated_at DESC"),
            "saved_all_time": jobs("SELECT * FROM jobs WHERE status='saved' ORDER BY updated_at DESC"),
            "skipped_in_period": jobs(
                f"SELECT * FROM jobs WHERE status='skipped' AND {period_jobs} ORDER BY updated_at DESC",
                (since_s,), 50),
            "ai_vetoed_in_period": jobs(
                f"SELECT * FROM jobs WHERE status='filtered' AND filter_reason LIKE 'ai:%' AND {period_jobs}"
                " ORDER BY score DESC", (since_s,), 50),
            "near_misses_in_period": jobs(
                f"SELECT * FROM jobs WHERE status='low' AND {period_jobs} ORDER BY score DESC",
                (since_s,), 15),
        },
        "config": _config_summary(cfg),
        "ats_companies": _ats(db),
    }
    report["issues"] = _issues(report, interrupted, cfg)
    return report


def _ats(db: DB) -> dict:
    try:
        from .collectors.ats import summary
        return summary(db)
    except Exception:  # noqa: BLE001 - never break the report
        return {}


def _config_summary(cfg: dict) -> dict:
    extra = cfg.get("extra_sources") or {}
    return {
        "min_score": (cfg.get("scoring") or {}).get("min_score"),
        "targets": [{"name": t.get("name"), "sites": t.get("sites")} for t in cfg.get("targets") or []
                    if t.get("enabled", True)],
        "extra_sources": sorted(k for k, v in extra.items() if (v or {}).get("enabled")),
        "ai": {k: (cfg.get("ai") or {}).get(k) for k in ("enabled", "max_reviews_per_run", "review_margin")},
        "schedule_hours": (cfg.get("schedule") or {}).get("interval_hours"),
    }


def _issues(rep: dict, interrupted: int, cfg: dict) -> list[dict]:
    out = []
    if not rep["runs"]:
        out.append({"level": "error", "what": "no runs in this period"})
    for name, a in rep["sources"].items():
        if a["status"] == "blocked_or_failing":
            out.append({"level": "error", "source": name,
                        "what": f"0 jobs in {a['runs']} run(s), {a['errors']} error(s)",
                        "last_error": a["last_error"]})
        elif a["status"] == "empty":
            out.append({"level": "warn", "source": name, "what": f"0 jobs in {a['runs']} run(s), no errors"})
        elif a["status"] == "degraded":
            out.append({"level": "warn", "source": name,
                        "what": f"{a['errors']} error(s) but still found {a['found']} jobs",
                        "last_error": a["last_error"]})
    if interrupted:
        out.append({"level": "warn", "what": f"{interrupted} run(s) killed before finishing (redeploy/crash)"})
    for reason, n in rep["ai"].get("off_reasons", {}).items():
        if reason != "not configured":
            out.append({"level": "warn", "source": "ai", "what": f"AI off in {n} run(s): {reason}"})
    if rep["ai"].get("failed"):
        out.append({"level": "warn", "source": "ai", "what": f"{rep['ai']['failed']} AI call(s) failed"})
    cap = int((cfg.get("telegram") or {}).get("max_per_run", 20))
    if len(rep["jobs"]["queued_now"]) > cap:
        out.append({"level": "info", "what": f"{len(rep['jobs']['queued_now'])} jobs queued, more than "
                                              f"one run sends ({cap})"})
    return out


def report_json(db: DB, cfg: dict, period: str | None = "24h") -> tuple[str, bytes]:
    rep = build_report(db, cfg, period)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    name = f"jobradar-report-{rep['report']['period']}-{stamp}.json"
    return name, json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8")


def caption(rep: dict) -> str:
    f, issues = rep["funnel"], rep["issues"]
    errs = sum(1 for i in issues if i["level"] == "error")
    warns = sum(1 for i in issues if i["level"] == "warn")
    return (f"📄 Report ({rep['report']['period']}): {len(rep['runs'])} runs · found {f.get('found', 0)} · "
            f"new {f.get('new', 0)} · sent {f.get('sent', 0)} · issues: {errs} errors, {warns} warnings")
