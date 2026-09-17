from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import (
    carbon_intensity,
    eac,
    elexon,
    elexon_bm,
    entsoe_flows,
    fuelinst,
    lccc,
    neso_embedded,
    pvlive,
    semo_flows,
    wind_curtailment,
)
from ..settlement import bid_data_published, week_dates
from ..storage import (
    BmCashflowRow,
    BmUnitReferenceRow,
    connect,
    log_fetch,
    upsert_bm_cashflows,
    upsert_bm_unit_reference,
    upsert_carbon_intensity,
    upsert_carbon_intensity_factors,
    upsert_eac_results,
    upsert_fuelinst,
    upsert_prices,
    wind_elexon_units,
)

# Every fetch_* call is a blocking HTTP request (requests releases the GIL
# while waiting on the socket), so these are I/O-bound, not CPU-bound --
# ThreadPoolExecutor is the right tool, not multiprocessing. Each worker
# opens its OWN sqlite3 connection (storage.connect() -- WAL mode + a 30s
# busy_timeout, see storage.py) rather than sharing one across threads:
# sqlite3.Connection objects aren't safe to use concurrently from multiple
# threads, and WAL was specifically added earlier for exactly this kind of
# concurrent-writer scenario. 8 workers is a practical default -- Elexon
# and NESO's public APIs aren't documented as rate-limiting, but going much
# wider risks tripping one, and network latency (not server throughput) is
# the actual bottleneck being addressed here.
DEFAULT_MAX_WORKERS = 8

SERIES = (
    "day_ahead", "imbalance", "wind", "total_generation", "demand",
    "solar", "demand_itsdo", "wind_forecast", "demand_forecast", "interconnector_actual",
)

EAC_SERIES = "eac"
EAC_CHUNK_DAYS = 7  # bounds each NESO request; also the resumability granularity

BM_CASHFLOW_SERIES = "bm_cashflow"
BM_REFERENCE_SERIES = "bm_unit_reference"

FUELINST_SERIES = "fuelinst"
FUELINST_WINDOW_MINUTES = 35  # generous enough to still catch the latest 5-min publish even if a refresh cycle runs a few minutes late

CARBON_INTENSITY_SERIES = "carbon_intensity"
CARBON_INTENSITY_FACTORS_SERIES = "carbon_intensity_factors"

IMRP_SERIES = "imrp"


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
        wind_rows, total_rows, interconnector_rows, note = elexon.fetch_generation(d)
        upsert_prices(conn, wind_rows)
        upsert_prices(conn, total_rows)
        upsert_prices(conn, interconnector_rows)
        log_fetch(conn, "wind", d, ok=True, note=note)
        log_fetch(conn, "interconnector_actual", d, ok=True, note=f"ok ({len(interconnector_rows)} rows)")
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "wind", d, ok=False, note=str(e))
        log_fetch(conn, "interconnector_actual", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_demand(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "demand", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "demand", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_demand_itsdo(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "demand_itsdo", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "demand_itsdo", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_wind_forecast(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "wind_forecast", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "wind_forecast", d, ok=False, note=str(e))

    try:
        rows, note = elexon.fetch_demand_forecast(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "demand_forecast", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "demand_forecast", d, ok=False, note=str(e))

    try:
        rows, note = pvlive.fetch_solar(d)
        upsert_prices(conn, rows)
        log_fetch(conn, "solar", d, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "solar", d, ok=False, note=str(e))


def ingest_week(conn: sqlite3.Connection, dates: list[date]) -> None:
    for d in dates:
        ingest_day(conn, d)


def ingest_week_parallel(db_path: Path | str, dates: list[date], max_workers: int = DEFAULT_MAX_WORKERS) -> None:
    """Same result as ingest_week(), but fetches every date's Elexon series
    concurrently instead of one date at a time -- the dominant cost of
    building a report is 40+ sequential days x 4 series of network
    round-trips, not local computation. Takes a db path rather than an open
    Connection since each worker thread needs its own connection.
    """
    def _one(d: date) -> None:
        worker_conn = connect(db_path)
        try:
            ingest_day(worker_conn, d)
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # list() forces every future to be waited on and any unexpected
        # exception (ingest_day itself never raises -- each series is its
        # own try/except -- but a connect() failure could) to surface here
        # rather than being silently dropped.
        list(pool.map(_one, dates))


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


def ingest_eac_range_parallel(
    db_path: Path | str, start: date, end: date, technology_type: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """Same chunking/idempotency/resumability as ingest_eac_range(), but
    every EAC_CHUNK_DAYS-sized chunk is fetched concurrently instead of one
    chunk at a time -- same rationale as ingest_week_parallel()."""
    chunks: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=EAC_CHUNK_DAYS - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    def _one(bounds: tuple[date, date]) -> None:
        chunk_start, chunk_end = bounds
        chunk_dates = [chunk_start + timedelta(days=i) for i in range((chunk_end - chunk_start).days + 1)]
        worker_conn = connect(db_path)
        try:
            try:
                rows = eac.fetch_range(chunk_start, chunk_end, technology_type)
                upsert_eac_results(worker_conn, rows)
                note = f"ok ({len(rows)} rows, {chunk_start}..{chunk_end})"
                for d in chunk_dates:
                    log_fetch(worker_conn, EAC_SERIES, d, ok=True, note=note)
            except Exception as e:  # noqa: BLE001 -- one bad chunk must not stop the rest
                for d in chunk_dates:
                    log_fetch(worker_conn, EAC_SERIES, d, ok=False, note=str(e))
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_one, chunks))


def _to_float(v: object) -> float | None:
    if v in (None, ""):
        return None
    return float(v)  # type: ignore[arg-type]


def ingest_embedded_forecasts(conn: sqlite3.Connection) -> None:
    """NESO's embedded solar + wind forecast (see ingest/neso_embedded.py)
    -- a single rolling-window fetch, not date-scoped, so this is called
    once per Live Market refresh cycle (background_refresh.py), not once
    per date the way ingest_day()'s series are. Both series come from one
    fetch, so one fetch_log entry covers both (a failure here means both
    are stale, not just one).
    """
    try:
        rows, note = neso_embedded.fetch_embedded_forecasts()
        upsert_prices(conn, rows)
        log_fetch(conn, "solar_forecast", date.today(), ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "solar_forecast", date.today(), ok=False, note=str(e))


def ingest_interconnector_scheduled(conn: sqlite3.Connection, today: date) -> None:
    """Scheduled interconnector flows for `today` -- continental links via
    ENTSO-E, Irish links via SEMO. Deliberately not folded into
    ingest_day(): unlike interconnector_actual (Phase 2, reuses FUELHH data
    ingest_day() already fetches for other reasons), this is Live-Market-
    only and would otherwise burden GB Power Weekly's historical report
    backfills (which call ingest_day() for dozens of unrelated past dates)
    with ENTSO-E/SEMO calls they have no use for. Called once per Live
    Market refresh cycle instead, same reasoning as ingest_embedded_forecasts().
    Each source logged independently -- e.g. a missing ENTSOE_KEY must not
    hide a working SEMO fetch, or vice versa.
    """
    try:
        rows, note = entsoe_flows.fetch_continental_scheduled(today)
        upsert_prices(conn, rows)
        log_fetch(conn, "interconnector_scheduled_entsoe", today, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "interconnector_scheduled_entsoe", today, ok=False, note=str(e))

    try:
        rows, note = semo_flows.fetch_semo_scheduled(today)
        upsert_prices(conn, rows)
        log_fetch(conn, "interconnector_scheduled_semo", today, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "interconnector_scheduled_semo", today, ok=False, note=str(e))


def ingest_wind_curtailment(conn: sqlite3.Connection, today: date) -> None:
    """Instructed-shut wind volume for today (see wind_curtailment.py),
    stored as its own series (wind_curtailed_mw) rather than mutating
    `wind` itself -- GB Power Weekly's wind-share-of-generation figures
    read `wind` as actual metered generation and must keep meaning that;
    Live Market's own display layer (live_market_metrics.
    actual_plus_addon_vs_forecast()) is what adds the two together for a
    "true" wind outturn view.

    Deliberately not folded into ingest_day(): ISPSTACK has no bulk
    multi-period endpoint (one call per settlement period), and GB Power
    Weekly's historical report backfills have no use for that per-period
    cost across dozens of unrelated past dates -- same reasoning as
    ingest_interconnector_scheduled(). Only attempts settlement periods
    `wind` already has real data for today (queried fresh here, not
    assumed) -- but which of those actually get stored is decided inside
    fetch_wind_curtailment() from the real response (a period Elexon
    hasn't published yet comes back completely empty, not just empty
    after filtering to wind), not from a fixed time estimate.
    bid_data_published() is only used here to skip periods that are
    obviously still in the future -- a cheap way to avoid firing off
    calls we're confident would be empty, not the thing that decides
    correctness.
    """
    try:
        sp_rows = conn.execute(
            "SELECT DISTINCT sp FROM prices WHERE series = 'wind' AND sd = ?", (today.isoformat(),)
        ).fetchall()
        periods = sorted(r[0] for r in sp_rows if bid_data_published(today, r[0]))
        wind_units = wind_elexon_units(conn)
        rows, note = wind_curtailment.fetch_wind_curtailment(today, periods, wind_units)
        upsert_prices(conn, rows)
        log_fetch(conn, "wind_curtailed_mw", today, ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "wind_curtailed_mw", today, ok=False, note=str(e))


def ingest_fuelinst(conn: sqlite3.Connection) -> None:
    """FUELINST generation-by-fuel-type for the Live Market Generation tab
    -- a trailing window, not date-scoped (see fuelinst.fetch_fuelinst()),
    called once per Live Market refresh cycle same as
    ingest_embedded_forecasts()/ingest_interconnector_scheduled(). Naturally
    self-limiting: display only ever reads "today so far", so there's no
    need to bound or prune the table here.
    """
    try:
        now = datetime.now(timezone.utc)
        rows, note = fuelinst.fetch_fuelinst(now - timedelta(minutes=FUELINST_WINDOW_MINUTES), now)
        upsert_fuelinst(conn, rows)
        log_fetch(conn, FUELINST_SERIES, date.today(), ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, FUELINST_SERIES, date.today(), ok=False, note=str(e))


def ingest_carbon_intensity(conn: sqlite3.Connection) -> None:
    """NESO Carbon Intensity API's headline figure + its official
    gCO2/kWh factors (see ingest/carbon_intensity.py) -- both endpoints
    always resolve to "now", so, like ingest_fuelinst(), this is called
    once per Live Market refresh cycle rather than being date-scoped.
    Logged as two series since they're two independent HTTP calls -- one
    failing must not hide the other succeeding. The factors barely
    change, but are re-ingested every cycle anyway rather than hardcoded
    -- carbon_intensity_metrics.emissions_mix() applies them to
    FUELINST's own generation mix for the donut's emissions-weighted %.
    """
    try:
        row, note = carbon_intensity.fetch_carbon_intensity()
        upsert_carbon_intensity(conn, row)
        log_fetch(conn, CARBON_INTENSITY_SERIES, date.today(), ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, CARBON_INTENSITY_SERIES, date.today(), ok=False, note=str(e))

    try:
        rows, note = carbon_intensity.fetch_carbon_intensity_factors()
        upsert_carbon_intensity_factors(conn, rows)
        log_fetch(conn, CARBON_INTENSITY_FACTORS_SERIES, date.today(), ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, CARBON_INTENSITY_FACTORS_SERIES, date.today(), ok=False, note=str(e))


def ingest_imrp(conn: sqlite3.Connection) -> None:
    """LCCC's Intermittent Market Reference Price (see ingest/lccc.py) --
    a single fetch-the-whole-table call, not date-scoped from this
    module's point of view (the table is only ~90k rows total and grows
    by 24/day, so re-fetching everything every cycle is cheap and always
    current -- same shape as ingest_embedded_forecasts(), not the
    EAC-style chunked _range()/_range_parallel() pair). This one call
    both keeps the series current *and* performs its own one-time
    backfill on the very first run.
    """
    try:
        rows, note = lccc.fetch_imrp()
        upsert_prices(conn, rows)
        log_fetch(conn, IMRP_SERIES, date.today(), ok=True, note=note)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, IMRP_SERIES, date.today(), ok=False, note=str(e))


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
                fuel_type=r.get("fuelType"),
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


def ingest_bm_cashflows_range_parallel(
    db_path: Path | str, start: date, end: date, max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """Same per-day bid+offer fetch as ingest_bm_cashflows_range(), but every
    date is fetched concurrently instead of one at a time -- same rationale
    as ingest_week_parallel()."""
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def _one(d: date) -> None:
        worker_conn = connect(db_path)
        try:
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
                upsert_bm_cashflows(worker_conn, rows)
                log_fetch(worker_conn, BM_CASHFLOW_SERIES, d, ok=True, note=f"ok ({len(rows)} rows)")
            except Exception as e:  # noqa: BLE001 -- one bad day must not stop the rest
                log_fetch(worker_conn, BM_CASHFLOW_SERIES, d, ok=False, note=str(e))
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_one, dates))
