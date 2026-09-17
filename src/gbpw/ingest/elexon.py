"""
Elexon Insights fetchers. Public, no key required. Base:
https://data.elexon.co.uk/bmrs/api/v1

Two endpoint families here, with different windowing behaviour (confirmed
by probing -- see scripts/probe_elexon.py):

  /balancing/...      filters by an ISO from/to on the record's own time,
                       or takes settlementDate as a path segment.
  /datasets/{code}     (FUELHH, INDO) ignores settlementDate/from/to entirely
                       and only responds to publishDateTimeFrom/publishDateTimeTo,
                       which filters on when the record was *published*, not
                       the settlement date it describes.

Because of that mismatch, every /datasets/ call below pads its publish-time
window by an hour either side of the target local day and then filters the
returned rows by their own settlementDate field, which Elexon computes
correctly against Europe/London (confirmed: a period with startTime
23:00Z carries settlementDate = the next UK calendar day, i.e. BST-aware).

Settlement run for imbalance prices: /balancing/settlement/system-prices/
{date} is documented by Elexon as always returning "the latest available
settlement run" for each period, with no run identifier in the payload.
There is no confirmed public endpoint for a *specific* historical run
(a candidate exists under a separate SAA API family but wasn't verified).
So we store whatever this endpoint currently calls latest, tagged with the
sentinel run='latest' (see storage.py). Re-running ingest naturally picks
up revisions as Elexon settlement runs progress.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..settlement import sp_start_utc, utc_to_settlement
from ..storage import NA_RUN, PriceRow

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# Elexon's generation-by-fuel-type categories. INT* are interconnector flows
# (imports/exports), not GB generation, so they're excluded from the
# generation total used for wind-share. Everything else -- including PS
# (pumped storage, which can be negative while charging) -- is treated as
# domestic generation, matching how NESO's own fuel-mix reporting frames it.
INTERCONNECTOR_PREFIX = "INT"
WIND_FUEL_TYPE = "WIND"

# fuelType code -> (series key, display name, counterparty country). Live-
# verified against a real FUELHH pull (all 10 codes seen in one sample) --
# not guessed from the notebook. series stored as
# f"interconnector_{key}_actual".
INTERCONNECTORS: dict[str, tuple[str, str, str]] = {
    "INTFR": ("ifa", "IFA", "FR"),
    "INTIFA2": ("ifa2", "IFA2", "FR"),
    "INTELEC": ("eleclink", "ElecLink", "FR"),
    "INTNED": ("britned", "BritNed", "NL"),
    "INTNEM": ("nemo", "Nemo", "BE"),
    "INTNSL": ("nsl", "North Sea Link", "NO"),
    "INTVKL": ("vikinglink", "Viking Link", "DK"),
    "INTGRNL": ("greenlink", "Greenlink", "IRL"),
    "INTEW": ("eastwest", "East-West", "IRL"),
    "INTIRL": ("moyle", "Moyle", "N.IRL"),
}


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


def _local_day_utc_bounds(d: date, pad_hours: int = 0) -> tuple[str, str]:
    start = sp_start_utc(d, 1) - timedelta(hours=pad_hours)
    end = sp_start_utc(d + timedelta(days=1), 1) + timedelta(hours=pad_hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_day_ahead(d: date) -> tuple[list[PriceRow], str]:
    """Market Index Data -> one blended GBP/MWh price per settlement period.

    Multiple data providers (APXMIDP, N2EXMIDP) can submit for the same
    period. We volume-weight across whichever providers reported volume>0
    for that period; a period with no positive-volume provider is skipped
    and reported in the returned note.
    """
    start, end = _local_day_utc_bounds(d)
    rows = _get(f"{BASE}/balancing/pricing/market-index", {"from": start, "to": end})
    rows = [r for r in rows if r["settlementDate"] == d.isoformat()]

    by_sp: dict[int, list[dict]] = {}
    for r in rows:
        by_sp.setdefault(r["settlementPeriod"], []).append(r)

    out: list[PriceRow] = []
    missing: list[int] = []
    for sp, entries in sorted(by_sp.items()):
        priced = [e for e in entries if e["volume"] > 0]
        if not priced:
            missing.append(sp)
            continue
        total_vol = sum(e["volume"] for e in priced)
        blended = sum(e["price"] * e["volume"] for e in priced) / total_vol
        out.append(PriceRow(series="day_ahead", sd=d, sp=sp, run=NA_RUN, value=blended))

    note = f"{len(missing)} period(s) with no priced MID provider: {missing}" if missing else "ok"
    return out, note


def fetch_imbalance(d: date) -> tuple[list[PriceRow], str]:
    """Also captures netImbalanceVolume (NIV) from this same response as
    series='imbalance_volume' -- zero extra calls, the field is already
    in the payload we fetch for price. NIV is Elexon/BSC's standard net
    imbalance volume for the settlement period, in MWh (well-established
    public terminology; unlike the EAC/NESO CKAN datasets used elsewhere
    in this project, this API exposes no per-field unit metadata to
    confirm it against directly).
    """
    rows = _get(f"{BASE}/balancing/settlement/system-prices/{d.isoformat()}", {})
    out: list[PriceRow] = []
    mismatches = 0
    for r in rows:
        if r["settlementDate"] != d.isoformat():
            continue
        if r["systemSellPrice"] != r["systemBuyPrice"]:
            mismatches += 1
        out.append(PriceRow(series="imbalance", sd=d, sp=r["settlementPeriod"], run="latest", value=r["systemSellPrice"]))
        if r.get("netImbalanceVolume") is not None:
            out.append(PriceRow(series="imbalance_volume", sd=d, sp=r["settlementPeriod"], run="latest", value=r["netImbalanceVolume"]))
    note = f"ok ({len(out)} periods)" if mismatches == 0 else f"ok, {mismatches} period(s) had SBP != SSP (used SSP)"
    return out, note


def fetch_generation(d: date) -> tuple[list[PriceRow], list[PriceRow], list[PriceRow], str]:
    """Returns (wind_rows, total_generation_rows, interconnector_rows, note).
    interconnector_rows are the same INT*-prefixed rows that were always
    being fetched here and discarded from the total -- now also kept, one
    series per named link (series="interconnector_<key>_actual"), zero new
    HTTP calls.
    """
    start, end = _local_day_utc_bounds(d, pad_hours=1)
    rows = _get(f"{BASE}/datasets/FUELHH", {"publishDateTimeFrom": start, "publishDateTimeTo": end})
    rows = [r for r in rows if r["settlementDate"] == d.isoformat()]

    wind_by_sp: dict[int, float] = {}
    total_by_sp: dict[int, float] = {}
    interconnector_by_key_sp: dict[tuple[str, int], float] = {}
    for r in rows:
        sp = r["settlementPeriod"]
        gen = r["generation"]
        fuel_type = r["fuelType"]
        if fuel_type == WIND_FUEL_TYPE:
            wind_by_sp[sp] = gen
        if not fuel_type.startswith(INTERCONNECTOR_PREFIX):
            total_by_sp[sp] = total_by_sp.get(sp, 0.0) + gen
        elif fuel_type in INTERCONNECTORS:
            key, _name, _country = INTERCONNECTORS[fuel_type]
            interconnector_by_key_sp[(key, sp)] = gen
        # An INT* code outside INTERCONNECTORS would mean a new/renamed link
        # Elexon started reporting -- silently dropped here rather than
        # crashing ingest, same as any other unexpected upstream field.

    wind_rows = [PriceRow(series="wind", sd=d, sp=sp, run=NA_RUN, value=v) for sp, v in sorted(wind_by_sp.items())]
    total_rows = [PriceRow(series="total_generation", sd=d, sp=sp, run=NA_RUN, value=v) for sp, v in sorted(total_by_sp.items())]
    interconnector_rows = [
        PriceRow(series=f"interconnector_{key}_actual", sd=d, sp=sp, run=NA_RUN, value=v)
        for (key, sp), v in sorted(interconnector_by_key_sp.items())
    ]
    note = f"ok ({len(wind_rows)} periods)"
    return wind_rows, total_rows, interconnector_rows, note


def fetch_demand(d: date) -> tuple[list[PriceRow], str]:
    start, end = _local_day_utc_bounds(d, pad_hours=1)
    rows = _get(f"{BASE}/datasets/INDO", {"publishDateTimeFrom": start, "publishDateTimeTo": end})
    rows = [r for r in rows if r["settlementDate"] == d.isoformat()]
    out = [PriceRow(series="demand", sd=d, sp=r["settlementPeriod"], run=NA_RUN, value=r["demand"]) for r in rows]
    note = f"ok ({len(out)} periods)"
    return out, note


def fetch_demand_itsdo(d: date) -> tuple[list[PriceRow], str]:
    """Transmission-level demand (ITSDO) -- distinct from `demand` (INDO),
    a genuine cross-check, not a duplicate."""
    start, end = _local_day_utc_bounds(d, pad_hours=1)
    rows = _get(f"{BASE}/datasets/ITSDO", {"publishDateTimeFrom": start, "publishDateTimeTo": end})
    rows = [r for r in rows if r["settlementDate"] == d.isoformat()]
    out = [PriceRow(series="demand_itsdo", sd=d, sp=r["settlementPeriod"], run=NA_RUN, value=r["demand"]) for r in rows]
    note = f"ok ({len(out)} periods)"
    return out, note


def fetch_wind_forecast(d: date) -> tuple[list[PriceRow], str]:
    """Wind generation forecast (WINDFOR). Rows carry no settlementDate/
    settlementPeriod field (unlike most /datasets/ endpoints) -- only an
    hourly startTime. Deriving (sd, sp) from that timestamp alone only
    ever lands on the *first* of the hour's two settlement periods (the
    odd one) -- confirmed live: a day's worth of stored rows was odd-SP
    only, every even SP silently empty. WINDFOR is genuinely hourly (one
    value per hour, not per half-hour), so each value covers *two*
    settlement periods -- the user's own Fundies.ipynb notebook handles
    this explicitly (cell 5's odd -> (odd, odd+1) sp_df expansion), ported
    here directly rather than left as a derived-period bug.

    Multiple published vintages cover the same period; every row is stored
    under run=<publishTime> rather than pre-filtered to "the latest" here
    -- series_for_week()'s MAX(run)-per-period logic (storage.py) already
    resolves that at read time.
    """
    start, end = _local_day_utc_bounds(d, pad_hours=1)
    rows = _get(f"{BASE}/datasets/WINDFOR", {"publishDateTimeFrom": start, "publishDateTimeTo": end})
    out: list[PriceRow] = []
    for r in rows:
        dt = datetime.strptime(r["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        sd, sp = utc_to_settlement(dt)
        if sd != d:
            continue
        for covered_sp in (sp, sp + 1):
            out.append(PriceRow(series="wind_forecast", sd=sd, sp=covered_sp, run=r["publishTime"], value=r["generation"]))
    note = f"ok ({len(out)} rows)"
    return out, note


def fetch_demand_forecast(d: date) -> tuple[list[PriceRow], str]:
    """Demand forecast (NDF, boundary=N -- national). Unlike every other
    /datasets/ endpoint used here, NDF rejects any publishDateTimeFrom/To
    window over 1 day (confirmed live: HTTP 400, "must not exceed 1 day"),
    so this uses the exact, unpadded local-day bounds rather than
    _local_day_utc_bounds()'s usual +/-1hr pad. Same run=<publishTime>
    multi-vintage storage as fetch_wind_forecast().
    """
    start, end = _local_day_utc_bounds(d, pad_hours=0)
    rows = _get(f"{BASE}/datasets/NDF", {"publishDateTimeFrom": start, "publishDateTimeTo": end, "boundary": "N"})
    rows = [r for r in rows if r["settlementDate"] == d.isoformat()]
    out = [
        PriceRow(series="demand_forecast", sd=d, sp=r["settlementPeriod"], run=r["publishTime"], value=r["demand"])
        for r in rows
    ]
    note = f"ok ({len(out)} rows)"
    return out, note
