"""
Wind curtailment (instructed shut-down volume) via Elexon's ISPSTACK bid
stack -- ported from the user's own already-working notebook logic.
FUELHH's `wind` series (elexon.py's fetch_generation()) only reports
actual metered generation; it has no way to show wind that was
CURTAILED (instructed off/down via the Balancing Mechanism for grid-
balancing reasons, confirmed live: real SO-flagged negative-volume bid
acceptances against named offshore wind farm units on a normal day). On
a day with heavy curtailment, `wind` alone understates what the wind
fleet could have generated -- making an actual-vs-forecast comparison
look like wind underperformed the resource forecast when really it was
told to stand down. Adding this back gives a "true" wind outturn figure
(see live_market_metrics.actual_plus_addon_vs_forecast()).

ISPSTACK has no per-technology filter and no bulk multi-period endpoint
-- one call per settlement period, filtered client-side to wind units
(storage.wind_elexon_units(), keyed by elexon_bm_unit -- ISPSTACK's own
`id` field uses that same code space, confirmed live against both the
live reference API and the NESO fuel-type spreadsheet).

Unit conversion (matches the notebook, and consistent with this
project's own earlier ISPSTACK cashflow reconciliation research):
`volume` is negative for a bid (turn-down) and already denominated in
MWh for that one settlement period -- multiplying by 2 converts that
half-hour energy figure into an average-MW rate directly comparable to
FUELHH's `generation` (MW). Multiple accepted rows for the same unit/
period (different bidOfferPairId levels) are summed, not averaged --
same discipline as the EBOCF/ISPSTACK cashflow reconciliation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from ..storage import NA_RUN, PriceRow

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
MAX_WORKERS = 16


def _fetch_bid_stack(d: date, sp: int) -> list[dict]:
    url = f"{BASE}/balancing/settlement/stack/all/bid/{d.isoformat()}/{sp}"
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json().get("data", [])
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


def fetch_wind_curtailment(d: date, periods: list[int], wind_units: set[str]) -> tuple[list[PriceRow], str]:
    """Total instructed-shut wind volume (MW-equivalent) for local day
    `d`, one row per settlement period in `periods` that Elexon has
    actually published bid data for -- callers pass periods that already
    have real FUELHH wind data (see ingest/__init__.py's
    ingest_wind_curtailment()), which may still include periods too fresh
    for ISPSTACK, so "published" is checked here against the real
    response, not assumed from a fixed publish-delay estimate.

    A settlement period's *full* bid stack (every BM unit, not just wind)
    has real acceptances all day, every day -- confirmed live: a period
    Elexon has genuinely published carries 70-180+ rows regardless of
    wind activity, while a period that simply hasn't been published yet
    comes back completely empty (0 rows, not "0 rows after filtering to
    wind"). That distinction matters: filtering to wind units first and
    treating an empty result as "zero curtailment" would be wrong for an
    unpublished period specifically, not just imprecise -- an 18-minute
    publish-delay estimate is only a rough guide for *when* to bother
    asking, never proof that an empty answer means zero. So a period
    with zero total rows is skipped here -- no row stored at all, not a
    fabricated zero -- and the same applies to a period whose fetch
    failed outright after retries: "unknown" is not "confirmed zero."
    """
    if not wind_units:
        return [], "skipped (no WIND units known -- run `gbpw load-fuel-types` first)"
    if not periods:
        return [], "ok (0 periods)"

    def _one(sp: int) -> tuple[int, list[dict] | None]:
        try:
            return sp, _fetch_bid_stack(d, sp)
        except Exception:  # noqa: BLE001 -- one bad period must not lose the rest
            return sp, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_one, periods))

    out: list[PriceRow] = []
    not_yet_published: list[int] = []
    failed: list[int] = []
    for sp, rows in results:
        if rows is None:
            failed.append(sp)
            continue
        if not rows:
            not_yet_published.append(sp)
            continue
        shut_mw = sum(abs(r["volume"]) * 2 for r in rows if r.get("id") in wind_units)
        out.append(PriceRow(series="wind_curtailed_mw", sd=d, sp=sp, run=NA_RUN, value=shut_mw))

    note = f"ok ({len(out)} periods)"
    if not_yet_published:
        note += f", {len(not_yet_published)} not yet published: {not_yet_published}"
    if failed:
        note += f", {len(failed)} failed: {failed}"
    return out, note
