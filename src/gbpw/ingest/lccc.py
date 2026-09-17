"""
Low Carbon Contracts Company (LCCC) Intermittent Market Reference Price
(IMRP) -- public, no key required, CKAN data portal
(dp.lowcarboncontracts.uk). IMRP is the reference price LCCC uses to
calculate CfD top-up payments for intermittent (wind/solar) generators,
and is "calculated using day-ahead data received from EPEX Spot and
NordPool" (LCCC's own field description) -- a genuine two-exchange blend,
unlike this project's existing `day_ahead` series (Elexon Market Index
Data), which is live-verified to be, in practice, a single-exchange
(APXMIDP) proxy: N2EXMIDP is priced in ~0% of periods across every month
checked. Fed into the PPA Tools page's capture-price calculation, not the
Live Market/Fundamentals `day_ahead` series, which stays untouched.

This CKAN portal's `datastore_search_sql` action 403s without a browser-
like User-Agent header; `datastore_search` (JSON, limit/offset) works
fine with one -- both live-confirmed. Own retry loop, same small pattern
as ingest/carbon_intensity.py/ingest/fuelinst.py, not a shared import.

The whole IMRP table is small (~90k rows for 2016-06-30..today, growing
by only 24 rows/day) and CKAN's own `datastore_search` returns a `total`
directly (no COUNT(*) OVER() window-function trick needed, unlike
ingest/eac.py's `datastore_search_sql` pagination) -- so this fetches the
*entire* table every call rather than chunking by date range. That also
means there's no separate historical-backfill step: the first ingest call
already pulls all of it (see ingest/__init__.py's ingest_imrp()).

IMRP's own settlement periods are HOURLY (1-24), not this project's usual
half-hourly (1-48) -- each source row is split into its two half-hourly
periods, same technique already used by ingest/elexon.py's
fetch_wind_forecast() and ingest/entsoe_flows.py's _hour_to_periods().
"""

from __future__ import annotations

import time
from datetime import date

import requests

from ..storage import NA_RUN, PriceRow

BASE = "https://dp.lowcarboncontracts.uk/api/3/action/datastore_search"
RESOURCE_ID = "d0e58993-d8b2-4ab5-90e0-518b1e7b2f1e"
PAGE_SIZE = 10_000  # table is ~90k rows total; loop below self-discovers the real cap via `total`
# Required -- live-confirmed this portal returns 403 on datastore_search_sql and on a bare
# datastore_search call without one; a browser-like UA fixes both.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gbpw-ingest)", "Accept": "application/json"}
TIMEOUT = 60
RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
SERIES = "imrp"


def _get(params: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(BASE, params=params, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"LCCC API error: {body}")
            return body["result"]
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def _fetch_all() -> list[dict]:
    result = _get({"resource_id": RESOURCE_ID, "limit": PAGE_SIZE, "offset": 0})
    records = result["records"]
    total = result["total"]
    offset = len(records)
    while offset < total:
        result = _get({"resource_id": RESOURCE_ID, "limit": PAGE_SIZE, "offset": offset})
        page = result["records"]
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def parse(records: list[dict]) -> list[PriceRow]:
    """Pure function, no network -- one source row (a full hour) becomes
    two PriceRows (that hour's two half-hourly settlement periods, same
    value on both). `IMRP_Date` carries a time-of-day component that's
    always 00:00:00 (the API dates every row to local midnight regardless
    of which hour it's for -- the real hour comes from `Settlement_Period`
    alone), so only the date portion is used.
    """
    out: list[PriceRow] = []
    for r in records:
        sd = date.fromisoformat(r["IMRP_Date"][:10])
        hour = int(r["Settlement_Period"])
        value = float(r["IMRP_Amount"])
        sp1 = (hour - 1) * 2 + 1
        out.append(PriceRow(series=SERIES, sd=sd, sp=sp1, run=NA_RUN, value=value))
        out.append(PriceRow(series=SERIES, sd=sd, sp=sp1 + 1, run=NA_RUN, value=value))
    return out


def fetch_imrp() -> tuple[list[PriceRow], str]:
    records = _fetch_all()
    rows = parse(records)
    note = f"ok ({len(records)} source rows -> {len(rows)} settlement-period rows)"
    return rows, note
