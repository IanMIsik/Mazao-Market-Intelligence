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
