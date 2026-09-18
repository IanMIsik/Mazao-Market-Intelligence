"""
Data layer for the PPA Tools page -- wind/solar capture price and capture
rate against LCCC's IMRP day-ahead reference (see ingest/lccc.py), plus
wind curtailment risk (already ingested for the Live Market Generation
tab, see ingest/wind_curtailment.py).

Works over an explicit (start, end) calendar range, same convention as
eac_metrics.py -- a capture-price window is naturally a calendar range
(this year, last 12 months, all-time), not a fixed week the way
settlement.week_dates()/storage.series_for_week() assume.

Capture price is the volume-weighted average price a technology's actual
generation achieved: sum(generation_mw * price) / sum(generation_mw) per
half-hour, against IMRP. The "baseload price" is the plain time-average
IMRP price over the same window -- what a flat, always-on seller would
get. capture_rate_pct = capture_price / baseload_price * 100; below 100%
means the technology tends to generate when the price is already low
(cannibalization), which is exactly what a wind/solar PPA negotiation
argues over. IMRP is used here, not the legacy `day_ahead` (Elexon Market
Index Data) series Live Market's Fundamentals tab reads -- IMRP is a
genuine two-exchange (EPEX Spot + NordPool) blend purpose-built as the
reference price for intermittent generators, while day_ahead is, in
practice, a single-exchange (APXMIDP) proxy (see ingest/lccc.py).
"""

from __future__ import annotations

import sqlite3
from datetime import date

SETTLEMENT_HOURS = 0.5  # each (sd, sp) row in `prices` is an average-MW rate for one 30-minute period

PRICE_SERIES = "imrp"
GENERATION_SERIES = {"wind": "wind", "solar": "solar"}
CURTAILMENT_SERIES = "wind_curtailed_mw"


def _capture_and_baseload(rows: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    """rows is [(generation_mw, price), ...] for the exact (sd, sp)
    periods where both the generation and price series have data.
    Returns (capture_price, baseload_price).

    Baseload is deliberately the plain average price over this SAME row
    set -- not a separately-scoped query over every period the price
    series has across the whole requested window. The generation and
    price series aren't ingested in lockstep (live-caught mid a multi-
    year backfill: solar had data for ~35 of ~123 possible months, while
    IMRP already covered the full window, so a window-wide baseload
    query averaged in ~7 years of solar-less months solar's own capture
    price never saw -- comparing 2016-2019's low prices against a
    baseload pulled from a mix including 2020s-inflated prices, yielding
    a meaningless >140% "capture rate"). Restricting baseload to the same
    joined rows fixes that regardless of how complete either series is.
    """
    if not rows:
        return None, None
    baseload = sum(price for _, price in rows) / len(rows)
    total_mwh = sum(mw for mw, _ in rows) * SETTLEMENT_HOURS
    if not total_mwh:
        return None, baseload
    weighted_revenue = sum(mw * price for mw, price in rows) * SETTLEMENT_HOURS
    return weighted_revenue / total_mwh, baseload


def _validate_technology(technology: str) -> str:
    if technology not in GENERATION_SERIES:
        raise ValueError(f"technology must be one of {sorted(GENERATION_SERIES)}, got {technology!r}")
    return GENERATION_SERIES[technology]


def capture_price(conn: sqlite3.Connection, technology: str, start: date, end: date) -> dict:
    """Capture price/rate for one technology over [start, end] (inclusive).
    `technology` is "wind" or "solar" -- the two series with a real,
    metered generation volume comparable against IMRP (itself LCCC's own
    Intermittent -- wind/solar -- reference price, so there's no third
    technology this comparison would even apply to).
    """
    gen_series = _validate_technology(technology)

    rows = conn.execute(
        """
        SELECT g.value, p.value
        FROM prices g
        JOIN prices p ON p.series = ? AND p.run = 'NA' AND p.sd = g.sd AND p.sp = g.sp
        WHERE g.series = ? AND g.run = 'NA' AND g.sd >= ? AND g.sd <= ?
        """,
        (PRICE_SERIES, gen_series, start.isoformat(), end.isoformat()),
    ).fetchall()
    total_generation_mwh = sum(mw for mw, _ in rows) * SETTLEMENT_HOURS
    capture, baseload = _capture_and_baseload(rows)

    rate = capture / baseload * 100 if capture is not None and baseload else None
    return {
        "technology": technology,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_generation_mwh": round(total_generation_mwh, 1),
        "capture_price_gbp_mwh": round(capture, 2) if capture is not None else None,
        "baseload_price_gbp_mwh": round(baseload, 2) if baseload is not None else None,
        "capture_rate_pct": round(rate, 1) if rate is not None else None,
    }


def capture_price_by_month(conn: sqlite3.Connection, technology: str, start: date, end: date) -> list[dict]:
    """Same figures as capture_price(), bucketed by calendar month --
    backs the PPA Tools page's capture-rate trend chart (charts_ppa.py).
    A month with no generation for this technology is simply omitted, not
    shown as a fabricated 0%.
    """
    gen_series = _validate_technology(technology)

    gen_rows = conn.execute(
        """
        SELECT substr(g.sd, 1, 7) AS month, g.value, p.value
        FROM prices g
        JOIN prices p ON p.series = ? AND p.run = 'NA' AND p.sd = g.sd AND p.sp = g.sp
        WHERE g.series = ? AND g.run = 'NA' AND g.sd >= ? AND g.sd <= ?
        ORDER BY month
        """,
        (PRICE_SERIES, gen_series, start.isoformat(), end.isoformat()),
    ).fetchall()

    by_month: dict[str, list[tuple[float, float]]] = {}
    for month, mw, price in gen_rows:
        by_month.setdefault(month, []).append((mw, price))

    out = []
    for month in sorted(by_month):
        # Same fix as capture_price(): baseload comes from this month's own
        # joined rows, not a separate query over every IMRP period in the
        # month regardless of whether this technology has data for all of it.
        capture, baseload = _capture_and_baseload(by_month[month])
        rate = capture / baseload * 100 if capture is not None and baseload else None
        out.append({
            "month": month,
            "capture_price_gbp_mwh": round(capture, 2) if capture is not None else None,
            "baseload_price_gbp_mwh": round(baseload, 2) if baseload is not None else None,
            "capture_rate_pct": round(rate, 1) if rate is not None else None,
        })
    return out


def _month_range(sparse_months: list[str]) -> list[str]:
    """Every calendar month from the earliest to the latest month present
    in the data, contiguous -- not just the sparse set of months that
    happen to have a value for either technology. Using this (rather
    than the sparse set directly) as the chart's x-axis domain keeps
    spacing genuinely proportional to elapsed time: live-caught mid-
    backfill, wind/solar data has had a real multi-month gap where
    indexing directly into the sparse set would silently compress that
    whole gap into one normal-width step, making the chart look like
    time passes at a uniform rate between plotted points when it
    doesn't. Moved here from the now-deleted web/charts_ppa.py -- this
    was originally the x-axis math for a hand-drawn SVG chart; it's the
    same fix, just feeding Chart.js's labels array instead of pixel
    coordinates now.
    """
    first_y, first_m = (int(part) for part in sparse_months[0].split("-"))
    last_y, last_m = (int(part) for part in sparse_months[-1].split("-"))
    out = []
    y, m = first_y, first_m
    while (y, m) <= (last_y, last_m):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def capture_rate_chart_data(wind_by_month: list[dict], solar_by_month: list[dict]) -> dict:
    """Shapes capture_price_by_month()'s output for the PPA Tools page's
    Chart.js line chart -- {"labels": [...], "wind": [...], "solar":
    [...]}, one entry per calendar month in _month_range()'s contiguous
    range. A month with no data for a technology is `None` (a real gap,
    e.g. not yet backfilled), not a fabricated 0% -- Chart.js's own
    spanGaps: false breaks the line there, same visual result as this
    project's own hand-drawn chart used to produce.
    """
    sparse_months = sorted({r["month"] for r in wind_by_month} | {r["month"] for r in solar_by_month})
    if not sparse_months:
        return {"labels": [], "wind": [], "solar": []}
    months = _month_range(sparse_months)
    wind_by = {r["month"]: r["capture_rate_pct"] for r in wind_by_month}
    solar_by = {r["month"]: r["capture_rate_pct"] for r in solar_by_month}
    return {
        "labels": months,
        "wind": [wind_by.get(m) for m in months],
        "solar": [solar_by.get(m) for m in months],
    }


def available_years(conn: sqlite3.Connection) -> list[int]:
    """Every calendar year from IMRP's (see ingest/lccc.py) earliest to
    latest reading -- the set of years the PPA Tools page's year filter
    offers. Anchored to IMRP specifically since that's the price series
    both capture_price() and the baseload figure are computed against;
    a year with IMRP data but sparse/no wind or solar generation for a
    technology simply falls through to this page's existing "no data"
    states for that figure, same as any other window already does.
    """
    row = conn.execute("SELECT MIN(sd), MAX(sd) FROM prices WHERE series = 'imrp'").fetchone()
    if not row or not row[0]:
        return []
    first_year, last_year = int(row[0][:4]), int(row[1][:4])
    return list(range(first_year, last_year + 1))


def curtailment_risk(conn: sqlite3.Connection, start: date, end: date) -> dict:
    """Wind curtailment over [start, end] -- how much of the wind fleet's
    potential output was instructed off via the Balancing Mechanism (see
    ingest/wind_curtailment.py, already ingested for the Live Market
    Generation tab's "true wind outturn" figure). Reused here as a PPA
    risk disclosure: who bears curtailment risk is a real point wind PPAs
    negotiate over.
    """
    curtailed_row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM prices WHERE series = ? AND run = 'NA' AND sd >= ? AND sd <= ?",
        (CURTAILMENT_SERIES, start.isoformat(), end.isoformat()),
    ).fetchone()
    actual_row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM prices WHERE series = 'wind' AND run = 'NA' AND sd >= ? AND sd <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()

    curtailed_mwh = curtailed_row[0] * SETTLEMENT_HOURS
    actual_mwh = actual_row[0] * SETTLEMENT_HOURS
    potential_mwh = actual_mwh + curtailed_mwh
    curtailed_pct = curtailed_mwh / potential_mwh * 100 if potential_mwh else None

    return {
        "curtailed_mwh": round(curtailed_mwh, 1),
        "actual_mwh": round(actual_mwh, 1),
        "potential_mwh": round(potential_mwh, 1),
        "curtailed_pct": round(curtailed_pct, 1) if curtailed_pct is not None else None,
    }


def _cfd_category(technology_type: str) -> str | None:
    """Substring match, not an exact-value dict -- LCCC's own
    Technology_Type spelling is inconsistent across rounds (live-verified:
    "Solar PV (> 5MW)" vs "Solar Photo-Voltaic (>5MW)" for the same
    category, "Remote Island Wind (> 5MW)" vs "Remote Island Wind (>5MW)"
    for another). Onshore/offshore/floating-offshore/Remote Island wind
    are all grouped as "wind" -- this page doesn't split wind by
    onshore/offshore anywhere else either (FUELINST's own `wind` series
    doesn't distinguish them), so this matches the page's existing
    granularity rather than inventing a finer split nothing else here uses.
    """
    t = technology_type.lower()
    if "solar" in t:
        return "solar"
    if "wind" in t:
        return "wind"
    return None


def cfd_benchmark(conn: sqlite3.Connection, technology: str) -> list[dict]:
    """Every CfD Allocation Round result for `technology` ("wind" or
    "solar"), one entry per round: capacity-weighted average strike
    price, total capacity, project count, and that round's own real
    price-base year (2012 or 2024 -- see ingest/cfd_auctions.py's
    PRICE_BASE_YEAR). Deliberately not converted to a common basis or to
    a nominal figure (no CPI/RPI indexation) -- by direct decision, not
    an oversight -- the page labels each round's figure with its own
    base year instead of implying rounds (or these figures and IMRP/
    capture price above) are on equal footing.
    """
    rows = conn.execute(
        "SELECT auction, technology_type, capacity_mw, strike_price_gbp_mwh, price_base_year "
        "FROM cfd_auction_outcomes"
    ).fetchall()

    by_round: dict[str, list[tuple[float, float, int]]] = {}
    for auction, tech_type, capacity_mw, strike_price, base_year in rows:
        if _cfd_category(tech_type) != technology:
            continue
        by_round.setdefault(auction, []).append((capacity_mw or 0.0, strike_price, base_year))

    out = []
    for auction in sorted(by_round, key=lambda a: int(a[2:])):  # "AR1".."AR7" numerically, not "AR10" < "AR2"
        entries = by_round[auction]
        total_capacity = sum(c for c, _, _ in entries)
        weighted_price = sum(c * p for c, p, _ in entries) / total_capacity if total_capacity else None
        out.append({
            "auction": auction,
            "price_base_year": entries[0][2],
            "project_count": len(entries),
            "total_capacity_mw": round(total_capacity, 1),
            "avg_strike_price_gbp_mwh": round(weighted_price, 2) if weighted_price is not None else None,
        })
    return out
