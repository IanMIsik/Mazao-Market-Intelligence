"""
ENTSO-E scheduled cross-border exchanges for the seven continental GB
interconnectors -- the three Irish links go through SEMO instead (see
semo_flows.py; Ireland isn't a clean ENTSO-E bidding-zone pair). Ported
from the user's own Fundies.ipynb notebook (PAIRS, fetch_pair_flows,
fetch_all_interconnectors_parallel) into this project's plain-function/
PriceRow convention, rather than reinvented -- all seven pairs were
live-verified against the real API with the user's own ENTSO-E key before
writing this.

Requires ENTSOE_KEY as a plain environment variable (an entso-e
Transparency Platform API key) -- same convention as ANTHROPIC_API_KEY in
narrative_llm.py; there is no .env mechanism in this project.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date

import pandas as pd
from entsoe import EntsoePandasClient

from ..settlement import utc_to_settlement
from ..storage import NA_RUN, PriceRow

# (country_code_from, country_code_to) -> our series key. IFA/IFA2/ElecLink
# are all reported under a GB<->FR country pair by ENTSO-E despite being
# three physically distinct links -- the GB_IFA/GB_IFA2/GB_ELECLINK
# bidding-zone codes (not the country pair) are what disambiguate them.
PAIRS: dict[tuple[str, str], str] = {
    ("GB_IFA2", "FR"): "ifa2",
    ("GB_IFA", "FR"): "ifa",
    ("GB_ELECLINK", "FR"): "eleclink",
    ("GB", "BE"): "nemo",
    ("GB", "NL"): "britned",
    ("GB", "NO"): "nsl",
    ("GB", "DK_1"): "vikinglink",
}


# entsoe-py's default is timeout=None, i.e. wait forever. On 2026-11-04 ENTSO-E stopped answering
# mid-request; the seven stuck fetch threads blocked the whole background refresh (so Live Market
# froze) for 30+ hours. Hence a real socket timeout, few retries, and an overall deadline below.
ENTSOE_TIMEOUT_SECONDS = 20
ENTSOE_RETRIES = 1
ENTSOE_RETRY_DELAY_SECONDS = 2
ENTSOE_DEADLINE_SECONDS = 150  # whole fetch, all seven links


def _client() -> EntsoePandasClient:
    key = os.environ.get("ENTSOE_KEY")
    if not key:
        raise RuntimeError("ENTSOE_KEY environment variable is not set")
    return EntsoePandasClient(
        api_key=key, retry_count=ENTSOE_RETRIES, retry_delay=ENTSOE_RETRY_DELAY_SECONDS, timeout=ENTSOE_TIMEOUT_SECONDS
    )


def _fetch_pair_net(
    client: EntsoePandasClient, pair: tuple[str, str], start: pd.Timestamp, end: pd.Timestamp
) -> pd.Series | None:
    """Net scheduled flow *into* pair[0] (always the GB-side code here) --
    matches this project's +import/-export-from-GB convention already used
    for interconnector_*_actual: flow_in - flow_out, where flow_in/flow_out
    are the two directed legs of the same pair. Falls back from intraday
    to day-ahead schedules on error, ported from fetch_pair_flows() --  not
    every link publishes an intraday schedule at every lead time.
    """
    from_country, to_country = pair
    for dayahead in (False, True):
        try:
            flow_out = client.query_scheduled_exchanges(
                country_code_from=from_country, country_code_to=to_country,
                start=start, end=end, dayahead=dayahead,
            )
            flow_in = client.query_scheduled_exchanges(
                country_code_from=to_country, country_code_to=from_country,
                start=start, end=end, dayahead=dayahead,
            )
            return flow_in - flow_out
        except Exception:  # noqa: BLE001 -- try the other lead time before giving up on this pair
            continue
    return None


def _hour_to_periods(ts: pd.Timestamp) -> list[tuple[date, int]]:
    """Each ENTSO-E row is one hourly value; GB settlement periods are 30
    minutes, so one hour covers two periods, both getting the same value
    (ENTSO-E doesn't publish sub-hourly schedules for these links).
    utc_to_settlement() (not the notebook's manual UK-hour arithmetic)
    handles the settlement-day boundary and DST correctly.
    """
    first = utc_to_settlement(ts.tz_convert("UTC").to_pydatetime())
    second = utc_to_settlement((ts + pd.Timedelta(minutes=30)).tz_convert("UTC").to_pydatetime())
    return [first, second]


def fetch_continental_scheduled(d: date) -> tuple[list[PriceRow], str]:
    """Scheduled flow for local day `d` across all seven continental
    links, fetched concurrently -- matches the notebook's own
    ThreadPoolExecutor approach (seven independent, I/O-bound round-trips).
    """
    client = _client()
    start = pd.Timestamp(d, tz="Europe/Brussels")
    end = start + pd.Timedelta(days=1)

    # Not `with ThreadPoolExecutor(...)`: its exit waits for every worker, so one hung request
    # would hang the caller. Wait up to a deadline, then abandon whatever is still running.
    pool = ThreadPoolExecutor(max_workers=len(PAIRS))
    futures = {pair: pool.submit(_fetch_pair_net, client, pair, start, end) for pair in PAIRS}
    wait(futures.values(), timeout=ENTSOE_DEADLINE_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)
    results = [
        (pair, f.result() if f.done() and not f.exception() else None) for pair, f in futures.items()
    ]

    out: list[PriceRow] = []
    failed: list[str] = []
    for pair, series in results:
        key = PAIRS[pair]
        if series is None:
            failed.append(key)
            continue
        for ts, value in series.items():
            if math.isnan(value):
                continue
            for sd, sp in _hour_to_periods(ts):
                if sd != d:
                    continue
                out.append(PriceRow(series=f"interconnector_{key}_scheduled", sd=sd, sp=sp, run=NA_RUN, value=float(value)))

    note = f"ok ({len(out)} rows)" if not failed else f"ok ({len(out)} rows), failed: {failed}"
    return out, note
