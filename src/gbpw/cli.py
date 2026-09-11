"""
Command-line entry point tying ingest / narrative / render / publish together.

    gbpw ingest     --week-ending 2026-08-30 [--history-days 37]
    gbpw ingest-eac --start 2026-08-01 --end 2026-09-11 [--technology Batteries]
    gbpw build      --week-ending 2026-08-30 --out out/gbpw-2026-08-30.html [--regenerate]
    gbpw publish    --week-ending 2026-08-30
    gbpw status     --week-ending 2026-08-30
    gbpw run        [--week-ending auto] [--out-dir out] [--regenerate]

`run` is the one-shot form meant for a scheduler: ingest the trailing window
then build, writing to <out-dir>/gbpw-<week-ending>.html. `--week-ending auto`
resolves to the most recently completed week (the Sunday on or before today).

`ingest-eac` is independent of the weekly report cycle -- it pulls NESO's
Enduring Auction Capability results for an arbitrary date range (chunked and
resumable internally, see ingest/__init__.py:ingest_eac_range), for the
BESS Analytics page rather than GB Power Weekly. A full 2-3 year backfill
and a short recent window use the same command, just a wider --start/--end.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .build import build_report, publish_report
from .ingest import ingest_eac_range, ingest_week
from .metrics import IncompleteWeekError, build_week
from .settlement import week_dates
from .storage import DEFAULT_DB_PATH, connect, get_report

logger = logging.getLogger("gbpw.cli")


def _week_ending(s: str) -> date:
    d = date.fromisoformat(s)
    if d.weekday() != 6:
        raise argparse.ArgumentTypeError(f"--week-ending must be a Sunday, got {s}")
    return d


def _most_recent_sunday(today: date) -> date:
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def _week_ending_or_auto(s: str) -> date | str:
    if s == "auto":
        return "auto"
    return _week_ending(s)


def _history_range(week_ending: date, history_days: int) -> list[date]:
    dates = week_dates(week_ending)
    history_start = dates[0] - timedelta(days=history_days)
    return [history_start + timedelta(days=n) for n in range((dates[-1] - history_start).days + 1)]


def _fetch_failures(conn, dates: list[date]) -> list[tuple]:
    """Failures from each (series, sd)'s *most recent* attempt only.

    fetch_log is append-only (a full audit trail), so a naive WHERE ok=0
    would resurface failures a later successful re-ingest already fixed.
    """
    sd_list = [d.isoformat() for d in dates]
    placeholders = ",".join("?" for _ in sd_list)
    return conn.execute(
        f"""
        SELECT f1.series, f1.sd, f1.note FROM fetch_log f1
        WHERE f1.ok = 0 AND f1.sd IN ({placeholders})
        AND f1.ts = (
            SELECT MAX(f2.ts) FROM fetch_log f2
            WHERE f2.series = f1.series AND f2.sd = f1.sd
        )
        ORDER BY f1.sd, f1.series
        """,
        sd_list,
    ).fetchall()


def _print_status(conn, week_ending: date, history_days: int) -> bool:
    """Prints an ingest/completeness summary for a week. Returns True if healthy."""
    dates = _history_range(week_ending, history_days)
    failures = _fetch_failures(conn, dates)
    healthy = True

    if failures:
        healthy = False
        print(f"{len(failures)} fetch failure(s) in range {dates[0]}..{dates[-1]}:")
        for series, sd, note in failures:
            print(f"  {sd} {series}: {note}")
    else:
        print(f"No fetch failures logged for {dates[0]}..{dates[-1]}.")

    try:
        build_week(conn, week_ending)
        print(f"Week ending {week_ending} is complete (all settlement periods present).")
    except IncompleteWeekError as e:
        healthy = False
        print(f"Week ending {week_ending} is INCOMPLETE:\n{e}")

    report = get_report(conn, week_ending)
    if report:
        print(f"Report on file: built_at={report['built_at']} published={report['published']}")
    else:
        print("No report built yet for this week.")

    return healthy


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="GB Power Weekly pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="fetch and store raw data for a week (plus trailing history)")
    p_ingest.add_argument("--week-ending", type=_week_ending, required=True)
    p_ingest.add_argument("--history-days", type=int, default=37)
    p_ingest.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_build = sub.add_parser("build", help="compute metrics, select narrative, render the HTML report")
    p_build.add_argument("--week-ending", type=_week_ending, required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_build.add_argument("--regenerate", action="store_true", help="force fresh narrative instead of reusing a stored one")
    p_build.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_publish = sub.add_parser("publish", help="mark a built report as published")
    p_publish.add_argument("--week-ending", type=_week_ending, required=True)
    p_publish.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_status = sub.add_parser("status", help="report ingest health and completeness for a week")
    p_status.add_argument("--week-ending", type=_week_ending, required=True)
    p_status.add_argument("--history-days", type=int, default=37)
    p_status.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_eac = sub.add_parser("ingest-eac", help="fetch and store NESO EAC results for a date range")
    p_eac.add_argument("--start", type=date.fromisoformat, required=True)
    p_eac.add_argument("--end", type=date.fromisoformat, required=True)
    p_eac.add_argument("--technology", default=None, help="e.g. Batteries -- omit to fetch every technology type")
    p_eac.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_run = sub.add_parser("run", help="ingest + build in one step, for a scheduler")
    p_run.add_argument("--week-ending", type=_week_ending_or_auto, default="auto")
    p_run.add_argument("--out-dir", type=Path, default=Path("out"))
    p_run.add_argument("--history-days", type=int, default=37)
    p_run.add_argument("--regenerate", action="store_true")
    p_run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    conn = connect(args.db)

    if args.command == "ingest":
        dates = _history_range(args.week_ending, args.history_days)
        print(f"Ingesting {len(dates)} days: {dates[0]} .. {dates[-1]}")
        ingest_week(conn, dates)
        failures = _fetch_failures(conn, dates)
        if failures:
            print(f"Done, with {len(failures)} failure(s):")
            for series, sd, note in failures:
                print(f"  {sd} {series}: {note}")
            sys.exit(1)
        print("Done, no failures.")

    elif args.command == "build":
        out_path = build_report(conn, args.week_ending, args.out, regenerate=args.regenerate)
        print(f"Wrote {out_path}")

    elif args.command == "publish":
        if publish_report(conn, args.week_ending):
            print(f"Marked week ending {args.week_ending} as published.")
        else:
            print(f"No report on file for week ending {args.week_ending} -- run `build` first.")
            sys.exit(1)

    elif args.command == "ingest-eac":
        n_days = (args.end - args.start).days + 1
        tech = args.technology or "all technologies"
        print(f"Ingesting EAC results for {n_days} day(s): {args.start}..{args.end} ({tech})")
        ingest_eac_range(conn, args.start, args.end, technology_type=args.technology)
        dates = [args.start + timedelta(days=i) for i in range(n_days)]
        sd_list = [d.isoformat() for d in dates]
        placeholders = ",".join("?" for _ in sd_list)
        failures = conn.execute(
            f"SELECT sd, note FROM fetch_log WHERE series = 'eac' AND ok = 0 AND sd IN ({placeholders}) "
            f"AND ts = (SELECT MAX(ts) FROM fetch_log f2 WHERE f2.series = 'eac' AND f2.sd = fetch_log.sd)",
            sd_list,
        ).fetchall()
        if failures:
            print(f"Done, with failures covering {len(failures)} day(s):")
            for sd, note in failures:
                print(f"  {sd}: {note}")
            sys.exit(1)
        count = conn.execute(
            f"SELECT COUNT(*) FROM eac_results WHERE sd IN ({placeholders})", sd_list
        ).fetchone()[0]
        print(f"Done, no failures. {count:,} row(s) on file for this range.")

    elif args.command == "status":
        healthy = _print_status(conn, args.week_ending, args.history_days)
        sys.exit(0 if healthy else 1)

    elif args.command == "run":
        week_ending = _most_recent_sunday(date.today()) if args.week_ending == "auto" else args.week_ending
        dates = _history_range(week_ending, args.history_days)
        print(f"Ingesting {len(dates)} days: {dates[0]} .. {dates[-1]}")
        ingest_week(conn, dates)
        failures = _fetch_failures(conn, dates)
        if failures:
            print(f"{len(failures)} fetch failure(s) -- see fetch_log. Continuing to build with what's available.")

        out_path = args.out_dir / f"gbpw-{week_ending.isoformat()}.html"
        try:
            build_report(conn, week_ending, out_path, regenerate=args.regenerate)
            print(f"Wrote {out_path}")
        except IncompleteWeekError as e:
            print(f"Could not build week ending {week_ending} -- incomplete data:\n{e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
