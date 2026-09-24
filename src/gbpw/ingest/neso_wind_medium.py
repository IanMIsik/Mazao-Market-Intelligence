"""
NESO's "14 Days Ahead Wind Forecasts" CKAN CSV -- a distinct product from
the "GB Embedded Wind and Solar Forecast" already used elsewhere
(neso_embedded.py): different methodology (national day-ahead incentive
wind forecast, evaluated against operational BM unit capacity), different
dataset, requested by name for the Forecasts page's wind panel. Same host
family (api.neso.energy) and shape (one whole-CSV download, no query
params, no publish-time field carried through except a single vintage per
row) as neso_embedded.py.

Like neso_embedded.py, this is a rolling window from *now* through ~14
days ahead, not a historical-range API -- refetching always returns
today's date as the earliest row, confirmed live (672 half-hourly rows,
14 days x 48 periods, spanning today through +14). Called once per Live
Market refresh cycle (background_refresh.py), not once per date.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date

import requests

from ..storage import PriceRow

# CKAN resource_id-keyed download link, same durability note as
# neso_embedded.py's URL -- the literal filename looks like a one-time
# export but this always serves the current rolling forecast.
URL = (
    "https://api.neso.energy/dataset/2f134a4e-92e5-43b8-96c3-0dd7d92fcc52/"
    "resource/93c3048e-1dab-4057-a2a9-417540583929/download/14da_wind_forecast.csv"
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


def fetch_wind_forecast_medium() -> tuple[list[PriceRow], str]:
    """Every (Date, Settlement_Period) row currently in the rolling
    0-14-day window, stored as series "wind_forecast_14d" -- kept separate
    from "wind_forecast" (WINDFOR, short-term) and "wind_embedded_forecast"
    (the embedded product) since all three are genuinely different NESO
    forecast products, not revisions of the same one. ForecastDateTime (the
    vintage this specific row was published under) is used as `run` --
    real Elexon-style multi-vintage storage, not this fetch's own clock,
    since the source actually carries one.
    """
    text = _get()
    reader = csv.DictReader(io.StringIO(text))

    out: list[PriceRow] = []
    for row in reader:
        sd = date.fromisoformat(row["Date"])
        sp = int(row["Settlement_Period"])
        out.append(PriceRow(series="wind_forecast_14d", sd=sd, sp=sp, run=row["ForecastDateTime"], value=float(row["Wind_Forecast"])))
    if out:
        dates = sorted({r.sd for r in out})
        note = f"ok ({len(out)} rows, {dates[0]}..{dates[-1]})"
    else:
        note = "ok (0 rows)"
    return out, note
