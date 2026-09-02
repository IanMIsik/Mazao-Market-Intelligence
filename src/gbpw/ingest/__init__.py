from __future__ import annotations

import sqlite3
from datetime import date

from . import elexon
from ..storage import log_fetch, upsert_prices

SERIES = ("day_ahead", "imbalance", "wind", "total_generation", "demand")


def ingest_day(conn: sqlite3.Connection, d: date) -> None:
    """Fetch every Elexon series for local settlement date `d` and upsert.

    Idempotent: safe to re-run for the same date. Each series is logged to
    fetch_log independently so a partial failure for one series doesn't
    hide success for the others.
    """
    try:
        rows, note = elexon.fetch_day_ahead(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "day_ahead", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001 -- ingest must not crash on one bad day
        log_fetch(conn, "day_ahead", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_imbalance(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "imbalance", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "imbalance", d, ok=False, note=str(e))

    try:
        wind_rows, total_rows, note = elexon.fetch_generation(d)
        upsert_prices(conn, wind_rows)
        upsert_prices(conn, total_rows)
        log_fetch(conn, "wind", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "wind", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_demand(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "demand", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "demand", d, ok=False, note=str(e))


def ingest_week(conn: sqlite3.Connection, week_dates: list[date]) -> None:
    for d in week_dates:
        ingest_day(conn, d)
