from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import eac, elexon
from ..storage import log_fetch, upsert_eac_results, upsert_prices

SERIES = ("day_ahead", "imbalance", "wind", "total_generation", "demand")

EAC_SERIES = "eac"
EAC_CHUNK_DAYS = 7  # bounds each NESO request; also the resumability granularity


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


def ingest_eac_range(
    conn: sqlite3.Connection, start: date, end: date, technology_type: str | None = None
) -> None:
    """Idempotent and resumable: fetched in EAC_CHUNK_DAYS-sized windows, each
    logged to fetch_log independently, so re-running after a partial failure
    only re-fetches the chunks that didn't already succeed -- important for a
    multi-year backfill, where one bad chunk shouldn't mean starting over.
    """
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=EAC_CHUNK_DAYS - 1), end)
        chunk_dates = [chunk_start + timedelta(days=i) for i in range((chunk_end - chunk_start).days + 1)]
        try:
            rows = eac.fetch_range(chunk_start, chunk_end, technology_type)
            upsert_eac_results(conn, rows)
            note = f"ok ({len(rows)} rows, {chunk_start}..{chunk_end})"
            for d in chunk_dates:
                log_fetch(conn, EAC_SERIES, d, ok=True, note=note)
        except Exception as e:  # noqa: BLE001 -- one bad chunk must not stop the rest
            for d in chunk_dates:
                log_fetch(conn, EAC_SERIES, d, ok=False, note=str(e))
        chunk_start = chunk_end + timedelta(days=1)
