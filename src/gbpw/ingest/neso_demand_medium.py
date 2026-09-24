"""
NESO's "2-14 Days Ahead Demand Forecast" CKAN CSV (half-hourly resource)
-- a distinct product from NDF (short-term, used elsewhere for Live
Market's demand forecast), covering the medium-term window this project's
day-ahead data doesn't reach. Same host family (api.neso.energy) as
neso_embedded.py/neso_wind_medium.py.

Genuinely starts at *today + 2 days*, not today or tomorrow -- confirmed
live (the dataset's own name is accurate: "2-14 days ahead", not
"1-14"). The Forecasts page's day-1 view is covered separately, by
Elexon's own NDF fetched a day ahead (see ingest/elexon.py's
fetch_demand_forecast_tomorrow()), not by this dataset at all.

Updated twice daily (09:20, 14:20 per the dataset's own metadata), so
called once per Live Market refresh cycle like the other rolling-window
NESO fetches here, not date-scoped.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timezone

import requests

from ..storage import PriceRow

URL = (
    "https://api.neso.energy/dataset/633daec6-3e70-444a-88b0-c4cef9419d40/"
    "resource/7c0411cd-2714-4bb5-a408-adb065edf34d/download/ng-demand-14da-hh.csv"
)
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


def _get() -> str:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(URL, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def _ctime_to_sp(ctime: str) -> int:
    """CTIME encodes the *end* of the settlement period as a plain HMM/HHMM
    integer string (no fixed width, no leading zeros) -- "30" is 00:30,
    "100" is 01:00, "2400" is 24:00 (SP48's own end) -- confirmed live
    against real rows. Always read the last two characters as minutes and
    whatever's left as hours (0 if nothing's left), then convert total
    minutes-from-midnight to a settlement period the same way every other
    half-hourly series here does (30-minute periods, 1-based).
    """
    ctime = str(ctime)
    minutes = int(ctime[-2:])
    hours = int(ctime[:-2]) if len(ctime) > 2 else 0
    total_minutes = hours * 60 + minutes
    return total_minutes // 30


def fetch_demand_forecast_medium() -> tuple[list[PriceRow], str]:
    """Every (DATE, CTIME) row currently in the rolling 2-14-day window,
    stored as series "demand_forecast_14d". No publish-vintage column in
    this file (unlike the wind medium-term CSV) -- run is this fetch's own
    UTC timestamp, same convention neso_embedded.py uses for the same
    reason.
    """
    text = _get()
    reader = csv.DictReader(io.StringIO(text))
    run = datetime.now(timezone.utc).isoformat()

    out: list[PriceRow] = []
    for row in reader:
        sd = date.fromisoformat(row["DATE"])
        sp = _ctime_to_sp(row["CTIME"])
        out.append(PriceRow(series="demand_forecast_14d", sd=sd, sp=sp, run=run, value=float(row["NATIONALDEMAND"])))
    if out:
        dates = sorted({r.sd for r in out})
        note = f"ok ({len(out)} rows, {dates[0]}..{dates[-1]})"
    else:
        note = "ok (0 rows)"
    return out, note
