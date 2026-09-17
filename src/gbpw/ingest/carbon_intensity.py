"""
NESO's Carbon Intensity API (api.carbonintensity.org.uk) -- public, no
key required, and a genuinely different host/API family from Elexon and
the NESO Data Portal CKAN CSVs used elsewhere in this project. Two
endpoints used here:

  /intensity          -- headline {forecast, actual, index} for the grid
                         overall, gCO2/kWh, always resolving to "now"
                         (confirmed live, no date/window params needed).
  /intensity/factors   -- the official gCO2/kWh factor per fuel type and
                         (for France/Netherlands/Ireland only) per import
                         country, live-verified: Biomass 120, Coal 937,
                         Gas CCGT 394, Gas OCGT 651, Hydro/Nuclear/Solar/
                         Wind/Pumped Storage 0, Oil 935, Other 300,
                         Dutch Imports 474, French Imports 53, Irish
                         Imports 458 -- essentially static, but fetched
                         rather than hardcoded to always match what NESO
                         currently publishes.

The donut's emissions-weighted breakdown is computed in
carbon_intensity_metrics.emissions_mix() from these factors applied to
FUELINST's own 5-minute generation mix (fuelinst_metrics.current_mix()),
not from this API's own /generation endpoint -- that endpoint has no
per-country import split, which the emissions breakdown needs (import
CI varies a lot by source grid).

Own retry loop, same small pattern as ingest/fuelinst.py and
ingest/wind_curtailment.py -- not a shared import.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from ..settlement import utc_to_settlement
from ..storage import CarbonIntensityFactorRow, CarbonIntensityRow

BASE = "https://api.carbonintensity.org.uk"
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


def _get(path: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(f"{BASE}{path}", timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()["data"]
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def _parse_from(from_str: str) -> datetime:
    return datetime.strptime(from_str, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def fetch_carbon_intensity() -> tuple[CarbonIntensityRow, str]:
    """The current half-hour's headline figure -- /intensity returns a
    single-element list, not a range, so there's nothing to filter here.
    """
    data = _get("/intensity")[0]
    sd, sp = utc_to_settlement(_parse_from(data["from"]))
    intensity = data["intensity"]
    row = CarbonIntensityRow(
        sd=sd, sp=sp, forecast=intensity["forecast"], actual=intensity.get("actual"), index_label=intensity["index"],
    )
    note = f"ok (SP{sp} {row.index_label})"
    return row, note


# /intensity/factors' own key names -> this project's own identifiers:
# FUELINST fuel type codes where one exists, "SOLAR" (not a FUELINST
# fuel type, matching fuelinst_metrics.py's own convention), or
# "IMPORT_<country>" for the 3 countries NESO publishes an import
# factor for. A factors response key not in this map (there shouldn't
# be one, but APIs change) is skipped rather than guessed at.
FACTOR_KEY_MAP: dict[str, str] = {
    "Biomass": "BIOMASS",
    "Coal": "COAL",
    "Gas (Combined Cycle)": "CCGT",
    "Gas (Open Cycle)": "OCGT",
    "Hydro": "NPSHYD",
    "Nuclear": "NUCLEAR",
    "Oil": "OIL",
    "Other": "OTHER",
    "Pumped Storage": "PS",
    "Solar": "SOLAR",
    "Wind": "WIND",
    "French Imports": "IMPORT_FR",
    "Dutch Imports": "IMPORT_NL",
    "Irish Imports": "IMPORT_IRL",
}


def fetch_carbon_intensity_factors() -> tuple[list[CarbonIntensityFactorRow], str]:
    """The official gCO2/kWh factor per fuel type/import country --
    /intensity/factors returns a single-element list of one big object,
    not a per-row list. Keys not in FACTOR_KEY_MAP are skipped rather
    than stored under a guessed identifier.
    """
    data = _get("/intensity/factors")[0]
    out = [
        CarbonIntensityFactorRow(key=FACTOR_KEY_MAP[name], factor=float(value))
        for name, value in data.items()
        if name in FACTOR_KEY_MAP
    ]
    note = f"ok ({len(out)} factors)"
    return out, note
