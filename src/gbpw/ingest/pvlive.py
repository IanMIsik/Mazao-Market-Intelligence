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

from ..settlement import LONDON, local_to_settlement, sp_start_utc
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

    PV_Live's own `datetime_gmt` is 30 minutes ahead of the settlement
    period it actually belongs to -- confirmed against the user's own
    already-working notebook code, which subtracts 0.5h (then applies a
    DST offset) before deriving settlementPeriod from the result. Without
    that shift, every row here would land one settlement period later
    than it should. This does the same correction via proper Europe/
    London zoneinfo conversion (utc_to_settlement()'s convention already
    used elsewhere in this package) rather than a manual DST-hours
    add, but the effect on the final (sd, sp) is identical. No change to
    the fetch window's start/end bounds is needed: shifting every
    timestamp back by 30 minutes just relabels which of the (period + 1)
    boundary rows this API always returns belongs to which period --
    confirmed by hand-tracing a full window, not assumed.
    """
    start = sp_start_utc(d, 1)
    end = sp_start_utc(d + timedelta(days=1), 1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    rows = _get({"start": start.strftime(fmt), "end": end.strftime(fmt)})

    out: list[PriceRow] = []
    for _gsp_id, dt_gmt, generation_mw in rows:
        dt_utc = datetime.strptime(dt_gmt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        local_dt = (dt_utc - timedelta(minutes=30)).astimezone(LONDON).replace(tzinfo=None)
        sd, sp = local_to_settlement(local_dt)
        if sd != d:
            continue
        out.append(PriceRow(series="solar", sd=sd, sp=sp, run=NA_RUN, value=generation_mw))
    note = f"ok ({len(out)} periods)"
    return out, note
