"""
SEMO (Ireland's Single Electricity Market Operator) scheduled flows for the
three GB<->Ireland interconnectors -- East-West, Moyle, Greenlink. Public,
no auth. Own module: different host (reports.sem-o.com) and shape from the
ENTSO-E client used for the seven continental links in entsoe_flows.py --
Ireland isn't a clean ENTSO-E bidding-zone pair, per the user's own
Fundies.ipynb notebook (process_ireland_flows()), ported here rather than
reinvented.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import requests

from ..settlement import utc_to_settlement
from ..storage import NA_RUN, PriceRow

BASE = "https://reports.sem-o.com/api/v1/dynamic/EA-012/total-scheduled-flows"
TIMEOUT = 30

# series key -> (field for flow *into* GB, field for flow *out of* GB).
# net = into - out, matching this project's +import/-export-from-GB
# convention already used for interconnector_*_actual -- confirmed against
# the notebook's process_ireland_flows(), which nets "towards UK" the same
# way. Field names verified live against a real SEM-O response.
_LINKS: dict[str, tuple[str, str]] = {
    "eastwest": ("TotalScheduled-IE-GB", "TotalScheduled-GB-IE"),
    "moyle": ("TotalScheduled-NI-GB", "TotalScheduled-GB-NI"),
    "greenlink": ("TotalScheduled-IE2-GB2", "TotalScheduled-GB2-IE2"),
}


def _fetch_auction(d: date, auction: str) -> list[dict]:
    params = {
        "StartTime": f">={d.isoformat()}",
        "EndTime": f"<={(d + timedelta(days=1)).isoformat()}",
        "page_size": 5000,
        "Auction": auction,
    }
    resp = requests.get(BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("items", [])


def fetch_semo_scheduled(d: date) -> tuple[list[PriceRow], str]:
    """IDA2 (the later, more current intraday auction) is preferred per
    settlement period; IDA1 fills any period IDA2 didn't cover -- same
    precedence as the notebook's process_ireland_flows()
    (Ida2.combine_first(Ida1)), just done with plain dicts keyed on
    StartTime rather than pandas.
    """
    ida1_items = _fetch_auction(d, "IDA1")
    ida2_items = _fetch_auction(d, "IDA2")

    by_start: dict[str, dict] = {item["StartTime"]: item for item in ida1_items}
    by_start.update({item["StartTime"]: item for item in ida2_items})  # IDA2 wins where present

    out: list[PriceRow] = []
    for start_time, item in by_start.items():
        dt = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        sd, sp = utc_to_settlement(dt)
        if sd != d:
            continue
        for key, (into_gb, out_of_gb) in _LINKS.items():
            net = item[into_gb] - item[out_of_gb]
            if math.isnan(net):
                continue
            out.append(PriceRow(series=f"interconnector_{key}_scheduled", sd=sd, sp=sp, run=NA_RUN, value=net))

    note = f"ok ({len(by_start)} periods x {len(_LINKS)} links)"
    return out, note
