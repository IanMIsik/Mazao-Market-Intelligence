"""
Elexon Balancing Mechanism cashflow data. Public, no key required. Base:
https://data.elexon.co.uk/bmrs/api/v1

Two endpoints, confirmed live before building this (see the plan's Context
section for the probe transcript):

- /reference/bmunits/all -- BM unit reference metadata (lead party, fuel
  type, registered capacity). Not date-scoped, changes slowly. Its
  nationalGridBmUnit codes (e.g. "AG-BUKP01") are the SAME code space as
  eac_results.auction_unit (e.g. "AG-ZEN03J") -- confirmed by direct
  comparison of live data -- so a battery identified via NESO's EAC data
  joins straight to its Elexon BM activity on this field.

- /balancing/settlement/indicative/cashflows/all/{bidOffer}/{settlementDate}
  (EBOCF) -- real GBP totalCashflow per BM unit per settlement period.
  {bidOffer} is a genuine filter, not a formality: the same BMU/period
  returns different, independently-nonzero cashflow for 'bid' vs 'offer'
  (confirmed live). Both must be fetched and summed for a unit's total BM
  revenue for a period. One call per day covers every settlement period
  that day (no settlementPeriod path segment needed, no pagination
  observed).

Cashflow only -- EBOCF carries no accepted-volume (MWh) figures. That needs
a separate ISPSTACK ingest, not built here (see storage.py's module
docstring and the plan's out-of-scope note).

Note on data quality, not fixed here: many battery BM units (particularly
V__-prefixed small/aggregated units) report generationCapacity as "0.000"
in the reference data. That's a real gap in Elexon's own data, not a bug in
this fetcher -- callers (bm_metrics.py) must treat a zero/missing capacity
as "unknown," never divide by it.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Literal

import requests

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
TIMEOUT = 60
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


def fetch_bmu_reference() -> list[dict]:
    """Every registered BM unit. Not date-scoped -- call this infrequently
    (e.g. once per ingest run), not per settlement date.
    """
    resp = requests.get(f"{BASE}/reference/bmunits/all", timeout=TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def fetch_cashflows(d: date, bid_offer: Literal["bid", "offer"]) -> list[dict]:
    """Every BM unit's EBOCF cashflow for every settlement period on `d`,
    for one side of the bid/offer ladder. Call for both 'bid' and 'offer'
    and combine -- see module docstring.
    """
    return _get(f"{BASE}/balancing/settlement/indicative/cashflows/all/{bid_offer}/{d.isoformat()}", {})
