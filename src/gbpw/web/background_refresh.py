"""
Periodic re-ingest of EAC + BM data for BESS Analytics.

The page's own meta refresh (see bess_analytics.html) only shows fresh data
if something is actually re-fetching it -- EAC clears daily and BM cashflow
settles continuously, so a page left open can go stale within one session
otherwise. A lightweight daemon thread, not a new scheduler dependency:
reuses the same ThreadPoolExecutor-based parallel ingest functions built
for GB Power Weekly's report build, just pointed at a short trailing window
instead of a whole report's history.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from pathlib import Path

from ..ingest import ingest_bm_cashflows_range_parallel, ingest_bmu_reference, ingest_eac_range_parallel
from ..storage import connect

logger = logging.getLogger("gbpw.web.background_refresh")

DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes, matches the page's own meta refresh
TRAILING_WINDOW_DAYS = 3  # EAC/BM data for the last few days can still be revised


def _refresh_once(db_path: Path) -> None:
    end = date.today()
    start = end - timedelta(days=TRAILING_WINDOW_DAYS)
    conn = connect(db_path)
    try:
        ingest_bmu_reference(conn)
    finally:
        conn.close()
    ingest_eac_range_parallel(db_path, start, end)
    ingest_bm_cashflows_range_parallel(db_path, start, end)
    logger.info("background refresh: re-ingested EAC + BM cashflows for %s..%s", start, end)


def start_background_refresh(db_path: Path, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> threading.Event:
    """Starts a daemon thread that calls _refresh_once() every
    interval_seconds, starting *after* the first interval elapses (not
    immediately on startup) -- deliberate, so `gbpw serve --reload` doesn't
    fire a real round of Elexon/NESO calls on every single code-save
    restart during development. Returns the stop Event so callers (tests,
    graceful shutdown) can end the loop early; daemon=True already means it
    won't block process exit on its own.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_seconds):
            try:
                _refresh_once(db_path)
            except Exception:  # noqa: BLE001 -- a bad refresh cycle must not kill the loop
                logger.exception("background refresh cycle failed, will retry next interval")

    thread = threading.Thread(target=_loop, name="bess-background-refresh", daemon=True)
    thread.start()
    return stop
