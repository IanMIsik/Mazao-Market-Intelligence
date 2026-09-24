"""
Command-line entry point tying ingest / narrative / render / publish together.

    gbpw ingest     --week-ending 2026-08-30 [--history-days 37]
    gbpw ingest-eac --start 2026-08-01 --end 2026-09-11 [--technology Batteries]
    gbpw ingest-bm  --start 2026-08-01 --end 2026-09-11
    gbpw build      --week-ending 2026-08-30 --out out/gbpw-2026-08-30.html [--regenerate]
    gbpw publish    --week-ending 2026-08-30
    gbpw status     --week-ending 2026-08-30
    gbpw run        [--week-ending auto] [--out-dir out] [--regenerate]
    gbpw serve      [--host 127.0.0.1] [--port 5000] [--reload]
    gbpw load-fuel-types --file BMUFuelType.xlsx
    gbpw ingest-cfd-auctions
    gbpw ingest-desnz-prices
    gbpw ingest-gdp-deflator

`run` is the one-shot form meant for a scheduler: ingest the trailing window
then build, writing to <out-dir>/gbpw-<week-ending>.html. `--week-ending auto`
resolves to the most recently completed week (the Sunday on or before today).

`ingest-eac` is independent of the weekly report cycle -- it pulls NESO's
Enduring Auction Capability results for an arbitrary date range (chunked and
resumable internally, see ingest/__init__.py:ingest_eac_range), for the
BESS Analytics page rather than GB Power Weekly. A full 2-3 year backfill
and a short recent window use the same command, just a wider --start/--end.

`ingest-bm` pulls Elexon's Balancing Mechanism cashflow data (EBOCF) for the
same page -- refreshes the BM unit reference table, then fetches bid+offer
cashflows day by day for the given range. Cashflow only, no accepted volumes
yet (see ingest/elexon_bm.py).

`serve` starts the FastAPI app (GB Power Weekly + BESS Analytics + Live Market) via uvicorn.

`load-fuel-types` rebuilds bm_unit_reference's fuel_type column from a
manually-downloaded NESO BM Unit Fuel Type spreadsheet merged with the live
reference API (see ingest/bmu_fuel_types.py) -- run this whenever a fresh
copy of that spreadsheet is downloaded. Powers wind curtailment on Live
Market (identifying which BM units are wind, see ingest/wind_curtailment.py).

`ingest-cfd-auctions` fetches LCCC's CfD Allocation Round strike-price
results (AR1 onward, see ingest/cfd_auctions.py) for the PPA Tools page.
Deliberately a manual command, not part of any automatic refresh cycle --
a new round's results are a rare, newsworthy event (roughly once or
twice a year), not something worth polling on a schedule. Run it again
whenever a new round (e.g. AR8) is published.

`ingest-desnz-prices` fetches DESNZ's Energy and Emissions Projections,
Annex M wholesale electricity price scenarios (see ingest/desnz_eep.py)
for the PPA Tools long-term price chart -- every known vintage in one
call. Manual, not scheduled -- DESNZ publishes a new edition every
several months to roughly a year. `ingest-gdp-deflator` fetches HM
Treasury's GDP deflator (see ingest/gdp_deflator.py), used alongside it
to rebase both that scenario data and LCCC's historical IMRP outturn
onto one "today's money" basis. Also manual -- Treasury publishes a new
release roughly quarterly, still rare enough not to poll. Both need to
have been run at least once for the long-term price chart to render.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .build import build_report, publish_report
from .ingest import (
    bmu_fuel_types,
    history_range as _history_range,
    ingest_bm_cashflows_range_parallel,
    ingest_bmu_reference,
    ingest_cfd_auction_outcomes,
    ingest_desnz_price_scenarios,
    ingest_eac_range_parallel,
    ingest_gdp_deflator,
    ingest_week_parallel,
)
from .metrics import IncompleteWeekError, build_week
from .settlement import most_recent_sunday as _most_recent_sunday
from .storage import DEFAULT_DB_PATH, connect, get_report, upsert_bm_unit_reference

logger = logging.getLogger("gbpw.cli")


def _week_ending(s: str) -> date:
    d = date.fromisoformat(s)
    if d.weekday() != 6:
        raise argparse.ArgumentTypeError(f"--week-ending must be a Sunday, got {s}")
    return d


def _week_ending_or_auto(s: str) -> date | str:
    if s == "auto":
        return "auto"
    return _week_ending(s)


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
        facts = build_week(conn, week_ending)
        if facts["day_ahead_gaps"]:
            print(f"Week ending {week_ending} builds, but with day-ahead gaps (tolerated, not fatal):")
            for gap in facts["day_ahead_gaps"]:
                print(f"  {gap['date']}: missing periods {gap['missing_periods']} (no priced MID trade)")
        else:
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

    p_bm = sub.add_parser("ingest-bm", help="fetch and store Elexon Balancing Mechanism cashflows for a date range")
    p_bm.add_argument("--start", type=date.fromisoformat, required=True)
    p_bm.add_argument("--end", type=date.fromisoformat, required=True)
    p_bm.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_fuel = sub.add_parser(
        "load-fuel-types",
        help="rebuild the BM unit -> fuel type mapping from NESO's fuel-type spreadsheet + the live reference API",
    )
    p_fuel.add_argument("--file", type=Path, required=True, help="path to the downloaded BMUFuelType*.xlsx")
    p_fuel.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_cfd = sub.add_parser(
        "ingest-cfd-auctions",
        help="fetch and store LCCC's CfD Allocation Round strike-price results (AR1 onward) -- run when a new round's results are published, not on a schedule",
    )
    p_cfd.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_desnz = sub.add_parser(
        "ingest-desnz-prices",
        help="fetch and store DESNZ's Annex M wholesale electricity price scenarios (all known vintages) -- run when a new edition is published, not on a schedule",
    )
    p_desnz.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_deflator = sub.add_parser(
        "ingest-gdp-deflator",
        help="fetch and store HM Treasury's GDP deflator, used to rebase the PPA Tools long-term price chart -- run when Treasury publishes a new release, not on a schedule",
    )
    p_deflator.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_run = sub.add_parser("run", help="ingest + build in one step, for a scheduler")
    p_run.add_argument("--week-ending", type=_week_ending_or_auto, default="auto")
    p_run.add_argument("--out-dir", type=Path, default=Path("out"))
    p_run.add_argument("--history-days", type=int, default=37)
    p_run.add_argument("--regenerate", action="store_true")
    p_run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_serve = sub.add_parser("serve", help="run the FastAPI dev server (uvicorn)")
    p_serve.add_argument("--host", default="127.0.0.1")
    # PORT env var takes precedence over the 5000 fallback, not the other
    # way round -- lets a process launcher that assigns its own port (no
    # --port flag passed) still be honored, while a plain `gbpw serve` with
    # neither still works exactly as before.
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    p_serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev only)")
    p_serve.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    conn = connect(args.db)

    if args.command == "ingest":
        dates = _history_range(args.week_ending, args.history_days)
        print(f"Ingesting {len(dates)} days: {dates[0]} .. {dates[-1]}")
        ingest_week_parallel(args.db, dates)
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
        ingest_eac_range_parallel(args.db, args.start, args.end, technology_type=args.technology)
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

    elif args.command == "ingest-bm":
        n_days = (args.end - args.start).days + 1
        print(f"Refreshing BM unit reference, then ingesting {n_days} day(s) of cashflows: {args.start}..{args.end}")
        ingest_bmu_reference(conn)
        ingest_bm_cashflows_range_parallel(args.db, args.start, args.end)
        dates = [args.start + timedelta(days=i) for i in range(n_days)]
        sd_list = [d.isoformat() for d in dates]
        placeholders = ",".join("?" for _ in sd_list)
        failures = conn.execute(
            f"SELECT sd, note FROM fetch_log WHERE series = 'bm_cashflow' AND ok = 0 AND sd IN ({placeholders}) "
            f"AND ts = (SELECT MAX(ts) FROM fetch_log f2 WHERE f2.series = 'bm_cashflow' AND f2.sd = fetch_log.sd)",
            sd_list,
        ).fetchall()
        if failures:
            print(f"Done, with failures covering {len(failures)} day(s):")
            for sd, note in failures:
                print(f"  {sd}: {note}")
            sys.exit(1)
        count = conn.execute(
            f"SELECT COUNT(*) FROM bm_cashflows WHERE sd IN ({placeholders})", sd_list
        ).fetchone()[0]
        print(f"Done, no failures. {count:,} row(s) on file for this range.")

    elif args.command == "load-fuel-types":
        print(f"Rebuilding BM unit fuel-type mapping from {args.file} + the live reference API...")
        rows = bmu_fuel_types.build_fuel_type_rows(args.file)
        n = upsert_bm_unit_reference(conn, rows, overwrite_fuel_type=True)
        wind_count = sum(1 for r in rows if r.fuel_type == "WIND")
        print(f"Done. {n:,} unit(s) on file, {wind_count:,} classified WIND.")

    elif args.command == "ingest-cfd-auctions":
        print("Fetching LCCC's CfD Allocation Round strike-price results...")
        ingest_cfd_auction_outcomes(conn)
        # This call's own log_fetch entry is necessarily the most recent one
        # for this series (only this command ever writes it) -- checking
        # THAT one row's ok value, not "has this series ever failed", so an
        # old failure doesn't get mistaken for this run's own result.
        last = conn.execute(
            "SELECT ok, note FROM fetch_log WHERE series = 'cfd_auction_outcomes' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last and not last[0]:
            print(f"Failed: {last[1]}")
            sys.exit(1)
        count = conn.execute("SELECT COUNT(*) FROM cfd_auction_outcomes").fetchone()[0]
        rounds = conn.execute("SELECT DISTINCT auction FROM cfd_auction_outcomes ORDER BY auction").fetchall()
        print(f"Done. {count:,} project result(s) on file across {len(rounds)} round(s): {', '.join(r[0] for r in rounds)}")

    elif args.command == "ingest-desnz-prices":
        print("Fetching DESNZ's Annex M wholesale electricity price scenarios (all known vintages)...")
        ingest_desnz_price_scenarios(conn)
        last = conn.execute(
            "SELECT ok, note FROM fetch_log WHERE series = 'desnz_price_scenarios' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last and not last[0]:
            print(f"Failed: {last[1]}")
            sys.exit(1)
        count = conn.execute("SELECT COUNT(*) FROM desnz_price_scenarios").fetchone()[0]
        vintages = conn.execute("SELECT DISTINCT vintage FROM desnz_price_scenarios ORDER BY vintage").fetchall()
        print(f"Done. {count:,} row(s) on file across {len(vintages)} vintage(s): {', '.join(v[0] for v in vintages)}")

    elif args.command == "ingest-gdp-deflator":
        print("Fetching HM Treasury's GDP deflator...")
        ingest_gdp_deflator(conn)
        last = conn.execute(
            "SELECT ok, note FROM fetch_log WHERE series = 'gdp_deflator' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last and not last[0]:
            print(f"Failed: {last[1]}")
            sys.exit(1)
        count = conn.execute("SELECT COUNT(*) FROM gdp_deflator").fetchone()[0]
        print(f"Done. {count:,} year(s) on file.")

    elif args.command == "status":
        healthy = _print_status(conn, args.week_ending, args.history_days)
        sys.exit(0 if healthy else 1)

    elif args.command == "run":
        week_ending = _most_recent_sunday(date.today()) if args.week_ending == "auto" else args.week_ending
        dates = _history_range(week_ending, args.history_days)
        print(f"Ingesting {len(dates)} days: {dates[0]} .. {dates[-1]}")
        ingest_week_parallel(args.db, dates)
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

    elif args.command == "serve":
        import uvicorn

        if args.reload:
            # --reload spawns a subprocess that re-imports the app, so it
            # needs an import string rather than an app instance; the db
            # path crosses that boundary via an env var (see
            # web/app.py:create_app_from_env).
            from .web.app import DB_PATH_ENV_VAR

            os.environ[DB_PATH_ENV_VAR] = str(args.db)
            # reload_dirs=[src] is load-bearing, not cosmetic: without it
            # uvicorn's watcher scans the whole project root, including
            # data/*.db. /gbpw/build writes to that db hundreds of times
            # during one ingest, and the watcher (a background thread)
            # burns CPU rescanning it on every write, contending for the
            # GIL with the request thread handling the actual build --
            # confirmed live, this alone made a ~3-minute ingest look
            # permanently hung (2700+ CPU-seconds burned in ~25 min
            # wall-clock, almost all of it the watcher, not the request).
            src_dir = str(Path(__file__).resolve().parents[1])
            uvicorn.run(
                "gbpw.web.app:create_app_from_env", factory=True,
                host=args.host, port=args.port, reload=True, reload_dirs=[src_dir],
            )
        else:
            from .web.app import create_app

            uvicorn.run(create_app(db_path=args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
