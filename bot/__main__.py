"""Entry point.

    python -m bot                 # run the Discord bot (scans every SCAN_INTERVAL_MINUTES)
    python -m bot scan            # one scan without Discord, prints new qualifying jobs (uses DATABASE_URL)
    python -m bot scan --temp-db  # same, but with a throwaway database (pure smoke test)
    python -m bot sources         # list configured sources
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from .config import load_settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _scan_once(args) -> int:
    from .app import RadarApp

    settings = load_settings()
    if args.temp_db:
        settings.database_url = "sqlite:///" + str(Path(tempfile.mkdtemp()) / "smoke.db")
    if args.sources_file:
        settings.sources_file = args.sources_file
    app = RadarApp(settings)
    if args.only:
        wanted = set(args.only.split(","))
        app.sources = [s for s in app.sources if s.kind in wanted or s.id in wanted]
        app.scanner.sources = app.sources
    try:
        report = await app.scanner.run("cli")
        print("\n=== Scan report ===")
        print(report.summary())
        if report.errors:
            print("\nFailed sources:")
            for sid, err in sorted(report.errors.items()):
                print(f"  ✗ {sid}: {err}")
        print("\nPer-source results:")
        for st in app.db.latest_source_status():
            mark = "✓" if st["ok"] else "✗"
            print(f"  {mark} {st['source']:<55} fetched={st['jobs_fetched']:<5} new={st['new_jobs']:<4} "
                  f"{st['duration_ms']}ms")
        if report.rejection_reasons:
            print("\nTop rejection reasons:")
            for reason, n in sorted(report.rejection_reasons.items(), key=lambda kv: -kv[1])[:10]:
                print(f"  {n:>5}  {reason}")
        jobs = [app.db.get_job(i) for i in report.new_job_ids]
        jobs = sorted([j for j in jobs if j], key=lambda j: -j.score)
        print(f"\nNew qualifying jobs: {len(jobs)} (showing up to {args.show})")
        for j in jobs[: args.show]:
            print(f"  [{j.id:>4}] {j.score:>3} {j.priority:<6} {j.company[:28]:<28} | {j.title[:60]:<60} | "
                  f"{(j.province or j.country or '?'):<7} | {j.term or '-':<12} | {j.url}")
        return 0 if report.sources_ok else 1
    finally:
        await app.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description="Canadian SWE Internship Radar")
    sub = parser.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("scan", help="run one scan without Discord")
    p_scan.add_argument("--temp-db", action="store_true", help="use a temporary database")
    p_scan.add_argument("--only", help="comma-separated source kinds or ids to include")
    p_scan.add_argument("--sources-file", help="override SOURCES_FILE")
    p_scan.add_argument("--show", type=int, default=40, help="max jobs to print")
    sub.add_parser("sources", help="list configured sources")
    p_exp = sub.add_parser("export", help="write every stored match to a file")
    p_exp.add_argument("--format", choices=["html", "csv"], default="html")
    p_exp.add_argument("--out", help="output file (default: internships.<format>)")
    args = parser.parse_args(argv)

    settings = load_settings()
    _setup_logging(settings.log_level)

    if args.cmd == "scan":
        return asyncio.run(_scan_once(args))
    if args.cmd == "export":
        from .db import Database
        from .export import to_csv, to_html

        db = Database(settings.database_path)
        jobs = db.all_jobs()
        data = to_html(jobs, settings.timezone) if args.format == "html" else to_csv(jobs, settings.timezone)
        out = Path(args.out or f"internships.{args.format}")
        out.write_bytes(data)
        db.close()
        print(f"Wrote {len(jobs)} jobs to {out.resolve()}")
        return 0
    if args.cmd == "sources":
        from .config import load_sources_config
        from .sources import build_sources

        sources, errors = build_sources(load_sources_config(settings.sources_file), include_disabled=True)
        for s in sources:
            print(f"{'on ' if s.enabled else 'off'}  {s.id:<60} {s.company}")
        for e in errors:
            print("config error:", e)
        print(f"\n{sum(s.enabled for s in sources)} enabled / {len(sources)} configured")
        return 0

    if not settings.discord_bot_token:
        print("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in (see README.md).\n"
              "You can still test sources without Discord: python -m bot scan --temp-db", file=sys.stderr)
        return 2
    from .discord_app import RadarBot

    bot = RadarBot(settings)
    bot.run(settings.discord_bot_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
