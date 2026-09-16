"""
Sheffield Solar PV_Live -- national embedded solar generation. Own module,
not elexon.py: different base URL, no auth, a genuinely separate source
(Elexon's own AGWS dataset also carries solar, but PV_Live is purpose-built
for this and was chosen over it -- see the Live Market Phase 2 plan).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..settlement import sp_start_utc, utc_to_settlement
from ..storage import NA_RUN, PriceRow

BASE = "https://api.pvlive.uk/pvlive/api/v4"
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
NATIONAL_GSP_ID = 0  # gsp_id=0 is the national (GB-wide) total, not a regional split


def _get(params: dict) -> list[list]:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(
                f"{BASE}/gsp/{NATIONAL_GSP_ID}", params=params, timeout=TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()["data"]
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def fetch_solar(d: date) -> tuple[list[PriceRow], str]:
    """National solar generation, one row per settlement period. Zero
    values (night-time) are real data, not gaps -- never skipped.
    """
    start = sp_start_utc(d, 1)
    end = sp_start_utc(d + timedelta(days=1), 1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    rows = _get({"start": start.strftime(fmt), "end": end.strftime(fmt)})

    out: list[PriceRow] = []
    for _gsp_id, dt_gmt, generation_mw in rows:
        dt = datetime.strptime(dt_gmt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        sd, sp = utc_to_settlement(dt)
        if sd != d:
            continue
        out.append(PriceRow(series="solar", sd=sd, sp=sp, run=NA_RUN, value=generation_mw))
    note = f"ok ({len(out)} periods)"
    return out, note
