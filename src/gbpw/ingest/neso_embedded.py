"""
NESO's "GB Embedded Wind and Solar Forecast" CKAN CSV -- the source used
for solar forecasting in the user's own Fundies.ipynb notebook, and (since
the CSV already carries an EMBEDDED_WIND_FORECAST column too) also the
Generation tab's "LV Wind" figure for GB Power Flow -- see
power_flow_metrics.py. Own module: different host (api.neso.energy) and
shape (one whole-CSV download, no query params at all) from
elexon.py/pvlive.py.

Unlike WINDFOR/NDF, this is not a historical-range API -- one fetch always
returns a rolling snapshot from *now* through ~14 days ahead (confirmed
live: refetching returns today's date every time, not a fixed calendar
range), and carries no publish-time field. That means:

- There's no point calling this once per historical date (see ingest_day())
  the way WINDFOR/NDF are -- every call returns the same rolling window
  regardless of what date you "meant" to ask for. It's called once per
  Live Market refresh cycle instead (background_refresh.py), same
  treatment as ingest_bmu_reference().
- Every fetch is stored under run=<this fetch's own UTC timestamp>, not a
  real upstream publish time -- series_for_week()'s MAX(run) resolution
  still does the right thing (always resolves to the most recently-fetched
  snapshot), it's just this module's own clock providing the ordering
  rather than NESO's.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timezone

import requests

from ..storage import PriceRow

# The filename segment here ("202506032012_embedded_forecast.csv") looks
# like a stale one-time export timestamp, but this is a CKAN
# resource_id-keyed download link -- confirmed live, it always serves the
# *current* forecast (today's date appears in the response) regardless of
# that literal filename. Not something to keep re-discovering; treated as
# a fixed, durable endpoint.
URL = (
    "https://api.neso.energy/dataset/91c0c70e-0ef5-4116-b6fa-7ad084b5e0e8/"
    "resource/db6c038f-98af-4570-ab60-24d71ebd0ae5/download/"
    "202506032012_embedded_forecast.csv"
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


def fetch_embedded_forecasts() -> tuple[list[PriceRow], str]:
    """Every (settlementDate, settlementPeriod) row currently in the
    rolling window -- today through ~14 days ahead, all in one HTTP call
    -- parsed into two series: EMBEDDED_SOLAR_FORECAST (as
    "solar_forecast") and EMBEDDED_WIND_FORECAST (as
    "wind_embedded_forecast"). The dataset's own name is "GB Embedded
    Wind and Solar Forecast" -- confirmed live it already carries both
    columns, so getting the second series costs nothing beyond parsing
    it. Callers filter to whatever date(s) they need; storing the full
    window costs nothing extra since it's already in hand.
    """
    text = _get()
    reader = csv.DictReader(io.StringIO(text))
    run = datetime.now(timezone.utc).isoformat()

    out: list[PriceRow] = []
    for row in reader:
        sd = date.fromisoformat(row["SETTLEMENT_DATE"][:10])
        sp = int(row["SETTLEMENT_PERIOD"])
        out.append(PriceRow(series="solar_forecast", sd=sd, sp=sp, run=run, value=float(row["EMBEDDED_SOLAR_FORECAST"])))
        out.append(PriceRow(series="wind_embedded_forecast", sd=sd, sp=sp, run=run, value=float(row["EMBEDDED_WIND_FORECAST"])))
    if out:
        dates = sorted({r.sd for r in out})
        note = f"ok ({len(out)} rows, {dates[0]}..{dates[-1]})"
    else:
        note = "ok (0 rows)"
    return out, note
