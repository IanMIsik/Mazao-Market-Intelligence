"""
NESO Enduring Auction Capability (EAC) results. Public, no key required.

    https://api.neso.energy/api/3/action/datastore_search_sql
    resource id: a63ab354-7e68-44c2-ad96-c6f920c30e85

Built from prior notebook research (see
reference/Enduring_Auction_Capability_Platorm_Research.ipynb). Two things
that research established, live-checked against the current API before
relying on them here:

- deliveryStart/deliveryEnd are Europe/London LOCAL civil time, not UTC.
  Confirmed by sampling live "Response" (frequency response) records: they
  land exactly on EFA block boundaries (22:00/02:00/06:00/10:00/14:00/18:00)
  even during BST, and the field metadata reports a timezone-naive
  `timestamp` type. So settlement period comes straight from the local
  hour/minute (settlement.local_to_settlement) -- no UTC conversion needed,
  unlike the Elexon fetchers in this same package.
- A single result can span more than one 30-minute settlement period
  (deliveryEnd - deliveryStart > 30 min). Each such record is expanded into
  one row per period before storing, so eac_results lines up at the same
  (sd, sp) granularity as the Elexon-derived `prices` table.

NESO asks for polite pagination: page size capped well under its ~32k
per-request hard limit, with a pause between page requests. A date range is
fetched in bounded internal chunks (see ingest/__init__.py's
ingest_eac_range) so a multi-year backfill is resumable rather than one
giant all-or-nothing call.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import requests

from ..settlement import local_to_settlement
from ..storage import EacRow

BASE = "https://api.neso.energy/api/3/action/datastore_search_sql"
RESOURCE_ID = "a63ab354-7e68-44c2-ad96-c6f920c30e85"
PAGE_SIZE = 30_000  # NESO's hard limit is ~32k rows per request
PAGE_SLEEP_SECONDS = 2.0  # polite pacing between paginated requests
TIMEOUT = 120
RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def _escape(s: str) -> str:
    return s.replace("'", "''")


def _fetch_page(start: date, end: date, offset: int, technology_type: str | None) -> tuple[list[dict], int]:
    where = [
        f'"deliveryStart" >= \'{start.isoformat()}T00:00:00\'',
        f'"deliveryStart" < \'{(end + timedelta(days=1)).isoformat()}T00:00:00\'',
    ]
    if technology_type:
        where.append(f'"technologyType" = \'{_escape(technology_type)}\'')
    sql = (
        f'SELECT COUNT(*) OVER () AS _count, * FROM "{RESOURCE_ID}" '
        f'WHERE {" AND ".join(where)} '
        f'ORDER BY "_id" ASC LIMIT {PAGE_SIZE} OFFSET {offset}'
    )

    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(BASE, params={"sql": sql}, timeout=TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"NESO API error: {body}")
            records = body["result"]["records"]
            total = int(records[0]["_count"]) if records else 0
            return records, total
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def _fetch_all(start: date, end: date, technology_type: str | None) -> list[dict]:
    records, total = _fetch_page(start, end, 0, technology_type)
    offset = len(records)
    while offset < total:
        time.sleep(PAGE_SLEEP_SECONDS)
        page, total = _fetch_page(start, end, offset, technology_type)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def _to_float(v: object) -> float | None:
    if v in (None, ""):
        return None
    return float(v)  # type: ignore[arg-type]


def expand(records: list[dict]) -> list[EacRow]:
    """One row per 30-minute settlement period, splitting any record whose
    delivery block spans more than one period. Pure function -- no network.
    """
    out: list[EacRow] = []
    for r in records:
        start = datetime.fromisoformat(r["deliveryStart"])
        end = datetime.fromisoformat(r["deliveryEnd"])
        n_slots = max(1, round((end - start).total_seconds() / 1800))

        for i in range(n_slots):
            slot_start = start + timedelta(minutes=30 * i)
            slot_end = slot_start + timedelta(minutes=30)
            sd, sp = local_to_settlement(slot_start)
            out.append(
                EacRow(
                    neso_id=int(r["_id"]),
                    unit_result_id=r.get("unitResultID"),
                    service_type=r["serviceType"],
                    auction_product=r["auctionProduct"],
                    technology_type=r.get("technologyType"),
                    auction_unit=r["auctionUnit"],
                    participant=r["registeredAuctionParticipant"],
                    executed_quantity=_to_float(r.get("executedQuantity")),
                    clearing_price=_to_float(r.get("clearingPrice")),
                    delivery_start=slot_start.isoformat(),
                    delivery_end=slot_end.isoformat(),
                    sd=sd,
                    sp=sp,
                    post_code=r.get("postCode"),
                )
            )
    return out


def fetch_range(start: date, end: date, technology_type: str | None = None) -> list[EacRow]:
    """Every EAC result with a delivery start on [start, end] (inclusive
    local dates), expanded to one row per settlement period.
    """
    records = _fetch_all(start, end, technology_type)
    return expand(records)
