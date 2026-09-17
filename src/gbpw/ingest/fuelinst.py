"""
Elexon FUELINST: generation by fuel type, published every 5 minutes --
confirmed live (consecutive publishTimes exactly 5 minutes apart). Row
shape: {publishTime, startTime, settlementDate, settlementPeriod,
fuelType, generation}. INT*-prefixed fuel types are interconnector flows,
not GB generation -- excluded here, same precedent as elexon.py's
fetch_generation() excluding them from total_generation. There is no
solar category at all (embedded solar isn't transmission-metered -- see
ingest/pvlive.py for how this project sources solar instead), so the
Generation Mix this feeds only ever reflects FUELINST's own fuel types.

Own retry loop, not a shared import from elexon.py's private _get() --
same pattern already used by wind_curtailment.py's _fetch_bid_stack().
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from ..storage import FuelInstRow
from .elexon import INTERCONNECTOR_PREFIX

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


def _get(url: str, params: dict) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()["data"]
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def fetch_fuelinst(window_start: datetime, window_end: datetime) -> tuple[list[FuelInstRow], str]:
    """One call to /datasets/FUELINST for a UTC publish-time window.

    Multiple rows in the window can carry the same (startTime, fuelType)
    -- a later publish revising an earlier one. Elexon returns rows in
    publish order, so the last one seen for a given (startTime, fuelType)
    wins here, matching storage.upsert_fuelinst()'s own "latest overwrites"
    semantics at the DB layer.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    rows = _get(f"{BASE}/datasets/FUELINST", {
        "publishDateTimeFrom": window_start.strftime(fmt),
        "publishDateTimeTo": window_end.strftime(fmt),
    })

    by_key: dict[tuple[str, str], float] = {}
    for r in rows:
        fuel_type = r["fuelType"]
        if fuel_type.startswith(INTERCONNECTOR_PREFIX):
            continue
        by_key[(r["startTime"], fuel_type)] = r["generation"]

    out = [
        FuelInstRow(
            start_time=datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
            fuel_type=fuel_type,
            generation_mw=gen,
        )
        for (start_time, fuel_type), gen in by_key.items()
    ]
    note = f"ok ({len(out)} rows)"
    return out, note
