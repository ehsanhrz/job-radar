from __future__ import annotations

import argparse
import logging
import random
import sys
import time

from .config import load_settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "JobSpy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(args, settings) -> None:
    from .pipeline import run_once
    run_once(settings, dry_run=args.dry_run, process_updates=not args.no_updates)


def cmd_loop(args, settings) -> None:
    from .pipeline import run_once
    sched = settings.section("schedule")
    hours = float(sched.get("interval_hours", 6))
    jitter = float(sched.get("jitter_minutes", 30))
    log = logging.getLogger("loop")
    while True:
        try:
            # in docker the bot container owns getUpdates, so don't fight over it
            run_once(settings, process_updates=False, trigger="loop")
        except Exception:  # noqa: BLE001
            log.exception("run failed")
        wait = hours * 3600 + random.uniform(-jitter, jitter) * 60
        log.info("next run in %.0f min", wait / 60)
        time.sleep(max(wait, 300))


def cmd_bot(args, settings) -> None:
    from .bot import run_bot
    run_bot(settings)


def cmd_serve(args, settings) -> None:
    from .serve import serve
    serve(settings)


def cmd_stats(args, settings) -> None:
    from .db import DB
    db = DB(settings.db_path)
    print("jobs by status:", db.stats())
    print("\nsource health (last 5 runs):")
    for s in db.source_summary():
        print(f"  {s['source']:<12} {s['found']:>5} jobs  {s['calls']:>3} calls  {s['errors']:>3} errors")
    print("\ntop filter reasons:")
    for r, c in db.top_filter_reasons(10):
        print(f"  {c:>5}  {r}")


def cmd_rescore(args, settings) -> None:
    """Re-apply filters/scoring to unsent jobs after you tweak config.yaml."""
    from .db import DB
    from .pipeline import rescore_all
    out = rescore_all(DB(settings.db_path), settings.raw)
    print(f"rescored: {out['changed']} jobs changed. queued now: {out['now_queued']}")


def cmd_probe(args, settings) -> None:
    """Check from THIS machine what each HTML board returns and what we parse."""
    from .collectors import html_boards as hb
    srcs = settings.section("extra_sources")
    for board, spec in hb.BOARDS.items():
        bcfg = srcs.get(board) or {}
        if not bcfg.get("enabled", False):
            print(f"\n=== {board}: disabled in config.yaml, skipped")
            continue
        s = hb.make_session(bcfg)
        urls = bcfg.get("list_urls") or []
        print(f"\n=== {board} ({len(urls)} list urls, client: {type(s).__module__.split('.')[0]})")
        first = None
        for url in urls:
            try:
                jobs = spec["parse_list"](hb._get(s, url))
                remote = sum(1 for j in jobs if j.is_remote)
                print(f"  OK   {len(jobs):>3} jobs ({remote} remote)  {url}")
                first = first or (jobs[0] if jobs else None)
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {type(e).__name__}: {e}  {url}")
        if first:
            print(f"  sample: {first.title} | {first.company} | {first.location} | remote={first.is_remote}")
            try:
                d = hb._fetch_detail(s, board, first)
                print(f"  detail: {len(d.get('description') or '')} chars of description, "
                      f"fields={sorted(k for k, v in d.items() if v)}")
            except Exception as e:  # noqa: BLE001
                print(f"  detail FAIL {type(e).__name__}: {e}")
    _probe_feeds(settings)


def cmd_report(args, settings) -> None:
    from .db import DB
    from .report import report_json
    name, data = report_json(DB(settings.db_path), settings.raw, args.period)
    out = args.output or name
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data) // 1024} KB)")


def _probe_feeds(settings) -> None:
    from .collectors import feeds
    res = feeds.collect(settings.raw)
    print("\n=== feeds (hn / remoteok / wwr)")
    for name, h in res.health.items():
        status = "OK  " if not h.errors else "FAIL"
        print(f"  {status} {name:<9} {h.found:>4} jobs  {h.last_error or ''}")
    for site in ("hn", "remoteok", "wwr"):
        j = next((x for x in res.jobs if x.site == site), None)
        if j:
            print(f"  sample {site}: {j.title[:70]} | {j.company} | {(j.location or '')[:50]} | remote={j.is_remote}")


def cmd_test_telegram(args, settings) -> None:
    from .telegram import Telegram
    tg = Telegram(settings.telegram_token, settings.telegram_chat_id, settings.telegram_proxy)
    me = tg.me()
    tg.send(f"👋 Job Radar is connected (bot @{me.get('username')}). Send /help.")
    print(f"OK: message sent via @{me.get('username')} to chat {settings.telegram_chat_id}")


def cmd_chat_id(args, settings) -> None:
    """Print chat ids of anyone who messaged the bot (to fill TELEGRAM_CHAT_ID)."""
    import requests
    if not settings.telegram_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first, then send any message to your bot.")
    s = requests.Session()
    if settings.telegram_proxy:
        s.proxies = {"http": settings.telegram_proxy, "https": settings.telegram_proxy}
    r = s.get(f"https://api.telegram.org/bot{settings.telegram_token}/getUpdates", timeout=30).json()
    seen = set()
    for u in r.get("result", []):
        chat = (u.get("message") or {}).get("chat")
        if chat and chat["id"] not in seen:
            seen.add(chat["id"])
            print(f"chat id: {chat['id']}  ({chat.get('username') or chat.get('title') or ''})")
    if not seen:
        print("No messages found. Send /start to your bot in Telegram, then run this again.")


def main() -> None:
    p = argparse.ArgumentParser(prog="jobradar", description="Job scraping pipeline -> Telegram")
    p.add_argument("-c", "--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scrape once, send new matches, exit")
    r.add_argument("--dry-run", action="store_true", help="print matches instead of sending")
    r.add_argument("--no-updates", action="store_true", help="don't process pending button taps")
    sub.add_parser("loop", help="run forever on the schedule in config.yaml")
    sub.add_parser("serve", help="bot + scheduler in one process (single-container hosting)")
    sub.add_parser("bot", help="always-on Telegram bot only (buttons + commands)")
    sub.add_parser("stats", help="print counts and source health")
    sub.add_parser("rescore", help="re-apply filters after editing config.yaml")
    sub.add_parser("test-telegram", help="send a test message")
    sub.add_parser("probe", help="check what the HTML boards and feeds return from this machine")
    rp = sub.add_parser("report", help="write the full JSON report (runs, sources, issues, jobs)")
    rp.add_argument("period", nargs="?", default="24h", help="24h | 7d | 2w | all")
    rp.add_argument("-o", "--output", help="file name (default: jobradar-report-<period>-<time>.json)")
    sub.add_parser("chat-id", help="find your TELEGRAM_CHAT_ID")

    args = p.parse_args()
    _setup_logging(args.verbose)
    settings = load_settings(args.config)
    {
        "run": cmd_run, "loop": cmd_loop, "serve": cmd_serve, "bot": cmd_bot, "stats": cmd_stats,
        "rescore": cmd_rescore, "test-telegram": cmd_test_telegram, "probe": cmd_probe, "report": cmd_report, "chat-id": cmd_chat_id,
    }[args.cmd](args, settings)


if __name__ == "__main__":
    main()
