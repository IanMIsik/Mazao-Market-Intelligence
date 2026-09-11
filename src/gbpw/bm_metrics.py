"""
Data layer for the Balancing Mechanism half of BESS Analytics -- Elexon's
EBOCF cashflow data (`bm_cashflows`) joined to whichever units NESO's EAC
data has already identified as batteries, via the shared national_grid_bm_unit
/ auction_unit code space (confirmed live -- see ingest/elexon_bm.py's
module docstring).

The battery-unit lookup goes through `eac_known_units` (a small, ingest-
maintained table -- see storage.py), not the raw multi-million-row
`eac_results` table. Measured on real data: with eac_results as the join
input, this query took ~5s (the query planner abandoned its indexes inside
the join and fell back to a full table scan); against eac_known_units
(~200 rows) the same query is ~milliseconds. Don't revert this to querying
eac_results directly without re-measuring.

Cashflow only. EBOCF carries no accepted-volume (MWh) figures -- that needs
a separate ISPSTACK ingest, not built yet (deferred per the user's explicit
direction; see storage.py's module docstring).

Bid and offer cashflow are kept separate throughout, not netted into a
single figure before this point: they're different market actions (offer =
accepted to increase output, bid = accepted to decrease it) and bid cashflow
is often negative by nature (confirmed live: market-wide bid total was net
negative, offer net positive, for the same window). A negative net total is
a real, expected outcome, not a computation error -- keeping both sides
alongside the net total is what makes that checkable.

Capacity handling: `bm_unit_reference.generation_capacity_mw` is 0 or NULL
for a real minority of matched battery units (confirmed live: 20 of 143 in
a 3-day sample) -- never divide by it blindly. gbp_per_mw_per_day is None
whenever capacity is unknown or zero; callers must show that honestly
("capacity not available"), never a fabricated ratio and never a unit
silently dropped.
"""

from __future__ import annotations

import sqlite3
from datetime import date

DEFAULT_TECHNOLOGY = "Batteries"
DAYS_EPSILON = 1e-9


def battery_bm_units(conn: sqlite3.Connection, technology_type: str | None = DEFAULT_TECHNOLOGY) -> list[str]:
    """Distinct auction_unit values for the given technology -- the
    national_grid_bm_unit codes this page's BM section is scoped to.
    """
    if technology_type is None:
        rows = conn.execute("SELECT DISTINCT auction_unit FROM eac_known_units").fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT auction_unit FROM eac_known_units WHERE technology_type = ?", (technology_type,)
        ).fetchall()
    return [r[0] for r in rows]


def _gbp_per_mw_per_day(total_revenue: float, capacity_mw: float | None, days: float) -> float | None:
    if capacity_mw is None or capacity_mw <= 0 or days <= DAYS_EPSILON:
        return None
    return total_revenue / capacity_mw / days


def bm_activity(
    conn: sqlite3.Connection, start: date, end: date, technology_type: str | None = DEFAULT_TECHNOLOGY, top_n: int = 8
) -> dict:
    days = (end - start).days + 1
    eac_where = "1=1" if technology_type is None else "technology_type = ?"
    eac_params = [] if technology_type is None else [technology_type]
    rows = conn.execute(
        f"""
        SELECT c.national_grid_bm_unit, c.bid_offer, SUM(c.total_cashflow) AS cashflow,
               r.lead_party_name, r.generation_capacity_mw
        FROM bm_cashflows c
        JOIN (SELECT DISTINCT auction_unit FROM eac_known_units WHERE {eac_where}) eac
          ON eac.auction_unit = c.national_grid_bm_unit
        LEFT JOIN bm_unit_reference r ON r.national_grid_bm_unit = c.national_grid_bm_unit
        WHERE c.sd >= ? AND c.sd <= ?
        GROUP BY c.national_grid_bm_unit, c.bid_offer
        """,
        [*eac_params, start.isoformat(), end.isoformat()],
    ).fetchall()

    by_unit: dict[str, dict] = {}
    for national_grid_bm_unit, bid_offer, cashflow, lead_party_name, capacity_mw in rows:
        entry = by_unit.setdefault(
            national_grid_bm_unit,
            {
                "national_grid_bm_unit": national_grid_bm_unit,
                "lead_party_name": lead_party_name,
                "bid_revenue_gbp": 0.0,
                "offer_revenue_gbp": 0.0,
                "generation_capacity_mw": capacity_mw,
            },
        )
        entry[f"{bid_offer}_revenue_gbp"] = cashflow or 0.0

    entries = []
    for entry in by_unit.values():
        total = entry["bid_revenue_gbp"] + entry["offer_revenue_gbp"]
        entry["total_revenue_gbp"] = total
        entry["gbp_per_mw_per_day"] = _gbp_per_mw_per_day(total, entry["generation_capacity_mw"], days)
        entries.append(entry)

    with_capacity = [e for e in entries if e["gbp_per_mw_per_day"] is not None]
    without_capacity = [e for e in entries if e["gbp_per_mw_per_day"] is None]
    with_capacity.sort(key=lambda e: e["gbp_per_mw_per_day"], reverse=True)
    without_capacity.sort(key=lambda e: e["total_revenue_gbp"], reverse=True)

    return {
        "total_revenue_gbp": sum(e["total_revenue_gbp"] for e in entries),
        "total_bid_revenue_gbp": sum(e["bid_revenue_gbp"] for e in entries),
        "total_offer_revenue_gbp": sum(e["offer_revenue_gbp"] for e in entries),
        "units_with_capacity": len(with_capacity),
        "units_without_capacity": len(without_capacity),
        "median_gbp_per_mw_day": _median(e["gbp_per_mw_per_day"] for e in with_capacity),
        "leaderboard": with_capacity[:top_n],
        "leaderboard_no_capacity": without_capacity[:top_n],
    }


def _median(values) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def unit_detail(conn: sqlite3.Connection, national_grid_bm_units: list[str], start: date, end: date) -> dict:
    """Per-unit bid/offer cashflow breakdown (summed over the window, not
    per settlement period -- the 'Selected participants' section shows a
    unit-level total, not a full time series) for the given BM units.
    """
    if not national_grid_bm_units:
        return {}
    days = (end - start).days + 1
    placeholders = ",".join("?" for _ in national_grid_bm_units)
    rows = conn.execute(
        f"""
        SELECT c.national_grid_bm_unit, c.bid_offer, SUM(c.total_cashflow), r.generation_capacity_mw
        FROM bm_cashflows c
        LEFT JOIN bm_unit_reference r ON r.national_grid_bm_unit = c.national_grid_bm_unit
        WHERE c.national_grid_bm_unit IN ({placeholders}) AND c.sd >= ? AND c.sd <= ?
        GROUP BY c.national_grid_bm_unit, c.bid_offer
        """,
        [*national_grid_bm_units, start.isoformat(), end.isoformat()],
    ).fetchall()

    out: dict[str, dict] = {
        u: {
            "bid_revenue_gbp": 0.0, "offer_revenue_gbp": 0.0, "total_revenue_gbp": 0.0,
            "generation_capacity_mw": None, "gbp_per_mw_per_day": None,
        }
        for u in national_grid_bm_units
    }
    for unit, bid_offer, total, capacity_mw in rows:
        entry = out[unit]
        entry[f"{bid_offer}_revenue_gbp"] = total or 0.0
        entry["generation_capacity_mw"] = capacity_mw

    for entry in out.values():
        entry["total_revenue_gbp"] = entry["bid_revenue_gbp"] + entry["offer_revenue_gbp"]
        entry["gbp_per_mw_per_day"] = _gbp_per_mw_per_day(entry["total_revenue_gbp"], entry["generation_capacity_mw"], days)

    return out
