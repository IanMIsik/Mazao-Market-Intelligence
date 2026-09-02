"""
Throwaway probe script. NOT part of the pipeline.

Calls each Elexon Insights endpoint we plan to use for 2026-08-26 and prints
the raw JSON shape, so we can agree the ingest design before writing it.

Usage: python scripts/probe_elexon.py
"""

import json
import urllib.request

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
DATE = "2026-08-26"


def get(url: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def show(label: str, url: str) -> None:
    print(f"\n{'=' * 70}\n{label}\nGET {url}\n{'=' * 70}")
    try:
        status, body = get(url)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read()[:500]}")
        return
    print(f"HTTP {status}")
    if isinstance(body, dict) and "data" in body:
        print("top-level keys:", sorted(body.keys()))
        print("metadata:", body.get("metadata"))
        print("record count:", len(body["data"]))
        if body["data"]:
            first = body["data"][0]
            print("record keys:", sorted(first.keys()))
            print("first record:", json.dumps(first, indent=2))
            if len(body["data"]) > 1:
                print("last record:", json.dumps(body["data"][-1], indent=2))
    else:
        print("unexpected shape:", json.dumps(body, indent=2)[:1000])


if __name__ == "__main__":
    # 1. Market Index Data (MID) -- day-ahead-ish reference price + volume,
    #    per data provider (APXMIDP, N2EXMIDP). Uses from/to as full ISO
    #    datetimes, half-open on the UTC day boundary.
    show(
        "Market Index Data (MID)",
        f"{BASE}/balancing/pricing/market-index"
        f"?from={DATE}T00:00:00Z&to={DATE}T23:59:59Z",
    )

    # 2. Settlement system prices (imbalance). Path-based settlementDate,
    #    NOT a query param. Per Elexon's own docs this ALWAYS collapses to
    #    the latest available settlement run -- no run identifier in the
    #    payload at all.
    show(
        "Settlement system prices (imbalance, DISEBSP)",
        f"{BASE}/balancing/settlement/system-prices/{DATE}",
    )
    show(
        "Settlement system prices with settlementRunType=II (ignored?)",
        f"{BASE}/balancing/settlement/system-prices/{DATE}?settlementRunType=II",
    )

    # 3. Generation by fuel type, half-hourly. This is a /datasets/{code}
    #    endpoint, a different family from /balancing/... above. It does
    #    NOT accept settlementDate or from/to -- only publishDateTimeFrom /
    #    publishDateTimeTo (confirmed by trial: settlementDate was silently
    #    ignored and it returned *today's* data instead).
    show(
        "Generation by fuel type, half-hourly (FUELHH) -- settlementDate param (WRONG, for comparison)",
        f"{BASE}/datasets/FUELHH?settlementDate={DATE}&settlementPeriod=1",
    )
    show(
        "Generation by fuel type, half-hourly (FUELHH) -- publishDateTimeFrom/To (correct)",
        f"{BASE}/datasets/FUELHH"
        f"?publishDateTimeFrom={DATE}T00:00:00Z&publishDateTimeTo={DATE}T23:59:59Z",
    )

    # 4. National demand (Initial National Demand Outturn). Same /datasets/
    #    family as FUELHH -- same publishDateTimeFrom/To requirement.
    show(
        "Initial National Demand outturn (INDO)",
        f"{BASE}/datasets/INDO"
        f"?publishDateTimeFrom={DATE}T00:00:00Z&publishDateTimeTo={DATE}T23:59:59Z",
    )
