"""
Periodic re-ingest for pages that need to look current between explicit
report builds: EAC + BM data for BESS Analytics, and today's core Elexon
series (day-ahead, imbalance, wind, demand) for the Live Market page.

Each page's own meta refresh (see bess_analytics.html, live_market.html)
only shows fresh data if something is actually re-fetching it -- EAC
clears daily, BM cashflow settles continuously, and today's settlement
periods keep clearing all day, so a page left open can go stale within one
session otherwise. A lightweight daemon thread, not a new scheduler
dependency: reuses the same ThreadPoolExecutor-based parallel ingest
functions built for GB Power Weekly's report build, just pointed at a
short trailing window (or, for Live Market, just today) instead of a
whole report's history.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import IO

from ..ingest import (
    ingest_bm_cashflows_range_parallel,
    ingest_bmu_reference,
    ingest_carbon_intensity,
    ingest_eac_range_parallel,
    ingest_embedded_forecasts,
    ingest_forecast_medium_term,
    ingest_fuelinst,
    ingest_imrp,
    ingest_interconnector_scheduled,
    ingest_week_parallel,
    ingest_wind_curtailment,
)
from ..storage import connect

logger = logging.getLogger("gbpw.web.background_refresh")

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes, per direct request -- matches both pages' own meta refresh
TRAILING_WINDOW_DAYS = 3  # EAC/BM data for the last few days can still be revised


def _refresh_once(db_path: Path) -> None:
    today = date.today()
    start = today - timedelta(days=TRAILING_WINDOW_DAYS)
    # EAC clears day-ahead -- NESO routinely publishes tomorrow's auction
    # results well before today is over (see routes_bess.py's auction_day
    # toggle), but a range ending at `today` only ever picks up tomorrow's
    # rows as an accident of EFA blocks that start today and run past
    # midnight (see ingest/eac.py's own delivery-block-splitting), never
    # the actual, complete day-ahead auction -- live-confirmed: NESO already
    # had ~9.7k genuine rows across 44 settlement periods for tomorrow while
    # this range only ever fetched ~870 spillover rows across 4. BM
    # cashflow only ever exists for periods that have already settled, so
    # its own range stays capped at `today`.
    eac_end = today + timedelta(days=1)

    conn = connect(db_path)
    try:
        ingest_bmu_reference(conn)
    finally:
        conn.close()
    ingest_eac_range_parallel(db_path, start, eac_end)
    ingest_bm_cashflows_range_parallel(db_path, start, today)
    logger.info("background refresh: re-ingested EAC for %s..%s, BM cashflows for %s..%s", start, eac_end, start, today)

    # Live Market only ever shows *today* live (the rest of its "week so
    # far" is already-settled data GB Power Weekly's own ingest covers) --
    # a single date is enough here, unlike the trailing window above.
    ingest_week_parallel(db_path, [today])
    logger.info("background refresh: re-ingested today's Live Market fundamentals (incl. solar/forecasts/interconnectors) for %s", today)

    # Not date-scoped (embedded forecasts, carbon intensity) or
    # Live-Market-only (scheduled interconnector flows) -- see
    # ingest_embedded_forecasts()/ingest_interconnector_scheduled()
    # docstrings for why these aren't folded into
    # ingest_week_parallel()/ingest_day() above.
    conn = connect(db_path)
    try:
        ingest_embedded_forecasts(conn)
        ingest_interconnector_scheduled(conn, today)
        # Must run after ingest_week_parallel() above -- it queries
        # today's already-ingested `wind` periods to know which
        # settlement periods are even worth an ISPSTACK call yet.
        ingest_wind_curtailment(conn, today)
        # FUELINST and the Carbon Intensity API both already publish on
        # the same 5-minute-or-finer cadence as this loop, so no separate
        # scheduling is needed -- they just join the rest of this
        # Live-Market-only block.
        ingest_fuelinst(conn)
        ingest_carbon_intensity(conn)
        # LCCC's IMRP table (PPA Tools' day-ahead price input, see
        # ingest/lccc.py) is only ~90k rows and updates once a day on
        # LCCC's side, so re-fetching the whole thing every 5-minute cycle
        # is more often than the source itself changes -- accepted here
        # rather than adding day-tracking logic, since a full fetch is
        # still just a handful of paginated requests and every write is
        # an idempotent upsert.
        ingest_imrp(conn)
        # Forecasts page (day 1..14 view, not shown on Live Market itself)
        # -- see ingest_forecast_medium_term()'s own docstring for why this
        # is four separate fetches, not folded into the block above.
        ingest_forecast_medium_term(conn, today)
    finally:
        conn.close()
    logger.info(
        "background refresh: re-ingested embedded forecasts + scheduled interconnector flows + "
        "wind curtailment + fuelinst + carbon intensity + IMRP + medium-term forecasts for %s", today,
    )


# Kept alive for the process's whole lifetime -- closing or garbage-
# collecting the handle releases the OS lock it holds (see
# _try_acquire_singleton_lock()). Only ever meaningfully non-None in one
# process at a time even when several share the same db_path.
_lock_handle: IO[bytes] | None = None


def _try_acquire_singleton_lock(lock_path: Path) -> IO[bytes] | None:
    """An OS-level advisory lock, not a marker *file* whose mere presence
    means "locked" -- the OS releases it automatically the instant the
    holding process exits, crashes, or is killed, so there's never a
    stale lock left behind after an unclean shutdown. That matters here:
    this app is restarted via `docker restart`/systemd, not a lock-aware
    orchestrator that would clean up a leftover marker file itself, and a
    permanently-stuck stale lock would silently stop the refresh loop
    from ever running again in any process.

    Returns the open file handle (caller must keep a reference to it for
    as long as the lock should be held) if this process won the lock, or
    None if another process already holds it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "wb")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def start_background_refresh(db_path: Path, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> threading.Event:
    """Starts a daemon thread that calls _refresh_once() immediately, then
    every interval_seconds after that.

    This used to wait out the first interval before ever refreshing, to
    avoid `gbpw serve --reload` firing a real round of Elexon/NESO/ENTSO-E
    calls on every single code-save restart during development. In
    practice that made a page look stale for a full 30 minutes after
    *every* server start -- including a genuinely fresh `gbpw serve` -- and
    during active development `--reload` restarts the app (and this
    thread) often enough that the 30-minute countdown routinely never
    survives to complete a single cycle, so the background refresh
    effectively never ran. Firing immediately trades a bit of extra API
    traffic during heavy edit-reload cycles for a page that's never stale
    right after startup -- the better trade for this project's actual
    usage pattern. Returns the stop Event so callers (tests, graceful
    shutdown) can end the loop early; daemon=True already means it won't
    block process exit on its own.

    Guarded by an OS-level advisory lock (data/.refresh.lock, next to the
    database) so only one process actually runs this loop when several
    processes share the same db_path -- e.g. `gbpw serve --workers N`, or
    simply starting the server twice by accident against the same file.
    Without this, every such process would run its own copy of the loop:
    N times the Elexon/NESO/ENTSO-E traffic for no benefit, all racing to
    upsert the same rows. A process that loses the race still returns a
    real, already-unused Event, so callers don't need two code paths.
    """
    global _lock_handle
    _lock_handle = _try_acquire_singleton_lock(db_path.parent / ".refresh.lock")
    stop = threading.Event()
    if _lock_handle is None:
        logger.info("background refresh already owned by another process for %s -- skipping in this one", db_path)
        return stop

    def _loop() -> None:
        while True:
            try:
                _refresh_once(db_path)
            except Exception:  # noqa: BLE001 -- a bad refresh cycle must not kill the loop
                logger.exception("background refresh cycle failed, will retry next interval")
            if stop.wait(interval_seconds):
                break

    thread = threading.Thread(target=_loop, name="bess-background-refresh", daemon=True)
    thread.start()
    return stop
