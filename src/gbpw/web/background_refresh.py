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
import threading
from datetime import date, timedelta
from pathlib import Path

from ..ingest import (
    ingest_bm_cashflows_range_parallel,
    ingest_bmu_reference,
    ingest_eac_range_parallel,
    ingest_interconnector_scheduled,
    ingest_solar_forecast,
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

    conn = connect(db_path)
    try:
        ingest_bmu_reference(conn)
    finally:
        conn.close()
    ingest_eac_range_parallel(db_path, start, today)
    ingest_bm_cashflows_range_parallel(db_path, start, today)
    logger.info("background refresh: re-ingested EAC + BM cashflows for %s..%s", start, today)

    # Live Market only ever shows *today* live (the rest of its "week so
    # far" is already-settled data GB Power Weekly's own ingest covers) --
    # a single date is enough here, unlike the trailing window above.
    ingest_week_parallel(db_path, [today])
    logger.info("background refresh: re-ingested today's Live Market fundamentals (incl. solar/forecasts/interconnectors) for %s", today)

    # Not date-scoped (solar forecast) or Live-Market-only (scheduled
    # interconnector flows) -- see ingest_solar_forecast()/
    # ingest_interconnector_scheduled() docstrings for why these aren't
    # folded into ingest_week_parallel()/ingest_day() above.
    conn = connect(db_path)
    try:
        ingest_solar_forecast(conn)
        ingest_interconnector_scheduled(conn, today)
        # Must run after ingest_week_parallel() above -- it queries
        # today's already-ingested `wind` periods to know which
        # settlement periods are even worth an ISPSTACK call yet.
        ingest_wind_curtailment(conn, today)
    finally:
        conn.close()
    logger.info("background refresh: re-ingested solar forecast + scheduled interconnector flows + wind curtailment for %s", today)


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
    """
    stop = threading.Event()

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
