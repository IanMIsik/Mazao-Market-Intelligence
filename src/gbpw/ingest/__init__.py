from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import eac, elexon, elexon_bm
from ..settlement import week_dates
from ..storage import (
    BmCashflowRow,
    BmUnitReferenceRow,
    log_fetch,
    upsert_bm_cashflows,
    upsert_bm_unit_reference,
    upsert_eac_results,
    upsert_prices,
)

SERIES = ("day_ahead", "imbalance", "wind", "total_generation", "demand")

EAC_SERIES = "eac"
EAC_CHUNK_DAYS = 7  # bounds each NESO request; also the resumability granularity

BM_CASHFLOW_SERIES = "bm_cashflow"
BM_REFERENCE_SERIES = "bm_unit_reference"


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


def ingest_week(conn: sqlite3.Connection, dates: list[date]) -> None:
    for d in dates:
        ingest_day(conn, d)


def history_range(week_ending: date, history_days: int) -> list[date]:
    """The target week's 7 dates plus `history_days` of trailing context
    (needed for the 30-day median spread figure -- see metrics.py). Shared
    by cli.py's `ingest`/`run`/`status` commands and the web `/gbpw/build`
    route so both build the exact same window for a given week.
    """
    dates = week_dates(week_ending)
    history_start = dates[0] - timedelta(days=history_days)
    return [history_start + timedelta(days=n) for n in range((dates[-1] - history_start).days + 1)]


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


def _to_float(v: object) -> float | None:
    if v in (None, ""):
        return None
    return float(v)  # type: ignore[arg-type]


def ingest_bmu_reference(conn: sqlite3.Connection) -> None:
    """Refreshes the whole BM unit reference table. Not date-scoped -- call
    once per ingest run, not once per day. Logged under a fixed sentinel
    date (today) since fetch_log's schema is date-shaped and this data
    isn't.
    """
    try:
        records = elexon_bm.fetch_bmu_reference()
        rows = [
            BmUnitReferenceRow(
                national_grid_bm_unit=r["nationalGridBmUnit"],
                elexon_bm_unit=r.get("elexonBmUnit"),
                lead_party_name=r.get("leadPartyName"),
                bm_unit_type=r.get("bmUnitType"),
                generation_capacity_mw=_to_float(r.get("generationCapacity")),
            )
            for r in records
            if r.get("nationalGridBmUnit")
        ]
        upsert_bm_unit_reference(conn, rows)
        log_fetch(conn, BM_REFERENCE_SERIES, date.today(), ok=True, note=f"ok ({len(rows)} units)")
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, BM_REFERENCE_SERIES, date.today(), ok=False, note=str(e))


def ingest_bm_cashflows_range(conn: sqlite3.Connection, start: date, end: date) -> None:
    """Day-by-day (EBOCF has no multi-day range endpoint): fetches both
    'bid' and 'offer' cashflows for each date and upserts both -- summing
    them is bm_metrics.py's job, not stored pre-summed, so bid/offer stay
    independently inspectable. Each day logged separately so a bad day
    doesn't block the rest, same discipline as ingest_day.
    """
    d = start
    while d <= end:
        try:
            rows: list[BmCashflowRow] = []
            for bid_offer in ("bid", "offer"):
                for r in elexon_bm.fetch_cashflows(d, bid_offer):
                    rows.append(
                        BmCashflowRow(
                            sd=date.fromisoformat(r["settlementDate"]),
                            sp=r["settlementPeriod"],
                            national_grid_bm_unit=r["nationalGridBmUnit"],
                            bid_offer=bid_offer,
                            total_cashflow=r["totalCashflow"],
                        )
                    )
            upsert_bm_cashflows(conn, rows)
            log_fetch(conn, BM_CASHFLOW_SERIES, d, ok=True, note=f"ok ({len(rows)} rows)")
        except Exception as e:  # noqa: BLE001 -- one bad day must not stop the rest
            log_fetch(conn, BM_CASHFLOW_SERIES, d, ok=False, note=str(e))
        d += timedelta(days=1)
