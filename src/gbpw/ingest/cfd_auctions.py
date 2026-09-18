"""
LCCC's CfD Allocation Round "Auction Outcomes" dataset -- per-project
strike prices from every completed Contracts for Difference round, AR1
onward. Same CKAN portal (dp.lowcarboncontracts.uk) and the same
required browser-like User-Agent header as ingest/lccc.py's IMRP fetcher
(that portal 403s without one on both datastore_search_sql and a bare
datastore_search call) -- but a different resource, a different row
shape (discrete per-project auction records, not a (sd, sp) time series,
so this does not produce PriceRows), and no reason to share more than
the request pattern with lccc.py, hence its own module.

Strike prices are NOT on a consistent price basis across rounds: AR1-AR6
are quoted in 2012 real prices, AR7 switched to 2024 real prices (live-
verified against a UK government CfD policy document -- AR6 figures need
inflating by roughly 39% just to sit on AR7's own basis, before even
comparing against something genuinely nominal like IMRP). PRICE_BASE_YEAR
below is this project's own explicit record of that fact per round --
it is not present in the source data itself. By direct decision (asked,
not assumed), this project does not convert between bases or to a
nominal figure (no CPI/RPI indexation) -- every stored/displayed price
keeps its own real base year label instead of implying rounds are on
equal footing. A round not in this mapping (e.g. a future AR8, not yet
published as of writing) is skipped by parse() rather than guessing its
basis -- extend this mapping only once verified against that round's own
DESNZ Pot and Price Notice.
"""

from __future__ import annotations

import time

import requests

from ..storage import CfdAuctionOutcomeRow

BASE = "https://dp.lowcarboncontracts.uk/api/3/action/datastore_search"
RESOURCE_ID = "55bd2d1e-d25d-4980-8569-ea27f6ec7217"
PAGE_SIZE = 1_000  # table is ~570 rows total; loop below self-discovers the real cap via `total`
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gbpw-ingest)", "Accept": "application/json"}
TIMEOUT = 60
RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

PRICE_BASE_YEAR: dict[str, int] = {
    "AR1": 2012, "AR2": 2012, "AR3": 2012, "AR4": 2012, "AR5": 2012, "AR6": 2012,
    "AR7": 2024,
}


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


def parse(records: list[dict]) -> list[CfdAuctionOutcomeRow]:
    """Pure function, no network. Skips any record whose Auction isn't in
    PRICE_BASE_YEAR (see module docstring) -- a round is either fully
    understood (its price basis confirmed) or not stored at all, never
    stored with a guessed basis.
    """
    out: list[CfdAuctionOutcomeRow] = []
    for r in records:
        auction = r["Auction"]
        if auction not in PRICE_BASE_YEAR:
            continue
        capacity = r.get("Capacity_MW")
        out.append(
            CfdAuctionOutcomeRow(
                lccc_id=int(r["_id"]),
                auction=auction,
                project_name=r["Project_Name"],
                developer=r.get("Developer") or None,
                technology_type=r["Technology_Type"],
                capacity_mw=float(capacity) if capacity else None,
                strike_price_gbp_mwh=float(r["Strike_Price_GBP_Per_MWh"]),
                price_base_year=PRICE_BASE_YEAR[auction],
                delivery_year=r.get("Delivery_Year") or None,
                region=r.get("Region") or None,
                publication_date=(r.get("Publication_Date") or "")[:10] or None,
            )
        )
    return out


def fetch_cfd_auction_outcomes() -> tuple[list[CfdAuctionOutcomeRow], str]:
    records = _fetch_all()
    rows = parse(records)
    skipped = len(records) - len(rows)
    note = f"ok ({len(records)} source rows -> {len(rows)} stored"
    note += f", {skipped} skipped -- round(s) not in PRICE_BASE_YEAR)" if skipped else ")"
    return rows, note
