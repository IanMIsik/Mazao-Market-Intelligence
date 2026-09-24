"""
Data layer for PPA Tools' "Long-term price outlook" card -- LCCC's IMRP
outturn history and DESNZ's Annex M scenario band on one money basis
(see ingest/desnz_eep.py and ingest/gdp_deflator.py for sourcing).

Everything here is expressed in one calendar year's money -- "today's
money", `to_year = date.today().year` by default -- via HM Treasury's
GDP deflator (ingest/gdp_deflator.py). Two different kinds of
conversion are involved, and they are NOT the same operation:

- IMRP outturn is genuinely nominal-at-the-time (what was actually
  settled), so each year needs true per-year deflation: value *
  deflator[to_year] / deflator[that year]. Aggregated to an ANNUAL mean
  (not monthly) specifically so it shares one calendar-year x-axis with
  DESNZ's own annual scenario granularity -- this page's charts are all
  Chart.js category-axis charts (no time-scale plugin loaded anywhere
  in this project), which can't otherwise align a "2026-09" point
  against a "2026" point.
- DESNZ's Annex M scenarios are already in DESNZ's own constant-price
  terms (2024 prices) -- rebasing a constant-price series to another
  year's money is a SINGLE ratio, deflator[to_year] / deflator[2024],
  applied uniformly across every year in the series. Hand-verified
  against the real published numbers before writing this (see the
  "long-term price outlook" plan): this ratio approach reproduces
  Vitreous Labs' own published figures (their inspiration for this
  chart) to within a rounding/vintage-lag difference; naively
  compounding *per-year* nominal inflation on top of an already-real
  series does not, and was ruled out for exactly that reason.

If `to_year` isn't in the deflator index (the current year has run
ahead of the latest ingested Treasury release, which only forecasts a
few years out), everything clamps to the latest year actually on file
-- `long_term_outlook()` surfaces that as `money_year` so the page
states the real basis plainly rather than mislabeling a clamped result
as "this year's money".
"""

from __future__ import annotations

import sqlite3
from datetime import date

DESNZ_BASE_YEAR = 2024  # DESNZ's own stated real-price basis for both known Annex M vintages (see ingest/desnz_eep.py)
SCENARIOS = ("reference", "ffp_low", "ffp_high")


def _deflator_index(conn: sqlite3.Connection, vintage: str | None = None) -> dict[int, float]:
    """The latest (or given) gdp_deflator vintage as {year: index}. Not
    date-scoped -- the whole small table for one vintage.
    """
    if vintage is None:
        row = conn.execute("SELECT MAX(vintage) FROM gdp_deflator").fetchone()
        vintage = row[0] if row else None
    if vintage is None:
        return {}
    rows = conn.execute("SELECT year, deflator FROM gdp_deflator WHERE vintage = ?", (vintage,)).fetchall()
    return {year: deflator for year, deflator in rows}


def _deflate(value: float, from_year: int, to_year: int, index: dict[int, float]) -> float | None:
    """value expressed in from_year's money -> to_year's money. None if
    either year's deflator index is missing -- a real, disclosed gap,
    never fabricated (see module docstring on why this is one ratio for
    an already-constant-price series, or a true per-year conversion for
    a nominal one -- the caller decides which `from_year` to pass).
    """
    if from_year not in index or to_year not in index:
        return None
    return round(value * index[to_year] / index[from_year], 2)


def _resolve_money_year(index: dict[int, float], today: date) -> int:
    """The year everything is expressed in -- today's calendar year, or
    the latest year actually on file if the deflator table hasn't been
    refreshed far enough forward yet (its forecast horizon is a few
    years, not indefinite). Never fabricates a year beyond what's on
    file; the caller discloses when this clamp happens.
    """
    if not index:
        return today.year
    return today.year if today.year in index else max(index)


def outturn_by_year(conn: sqlite3.Connection, to_year: int, index: dict[int, float]) -> list[dict]:
    """Annual mean of the `imrp` series (see ingest/lccc.py), deflated
    into to_year's money -- annual, not monthly, specifically so it
    shares one calendar-year x-axis with DESNZ's own annual scenario
    granularity below (a category-axis Chart.js chart, like every other
    chart on this page, can't otherwise align a "2026-09" outturn point
    against a "2026" scenario point without a genuine time scale, which
    this project doesn't load anywhere). A year whose deflator isn't in
    `index` is omitted, not shown un-deflated.
    """
    rows = conn.execute(
        "SELECT substr(sd, 1, 4) AS year, AVG(value) "
        "FROM prices WHERE series = 'imrp' AND run = 'NA' GROUP BY year ORDER BY year"
    ).fetchall()
    out = []
    for year, avg_value in rows:
        deflated = _deflate(avg_value, int(year), to_year, index)
        if deflated is not None:
            out.append({"year": int(year), "value": deflated})
    return out


def available_vintages(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT vintage FROM desnz_price_scenarios ORDER BY vintage").fetchall()
    return [r[0] for r in rows]


def scenarios_for_vintage(conn: sqlite3.Connection, vintage: str, to_year: int, index: dict[int, float]) -> dict:
    """{"reference": [{"year", "value"}, ...], "ffp_low": [...], "ffp_high": [...]}
    for one vintage, each value rebased by the single from-price_base_year
    ratio (see module docstring). A vintage whose price_base_year isn't
    in `index` produces empty series for that vintage rather than a
    partially-deflated one.
    """
    rows = conn.execute(
        "SELECT scenario, year, value_gbp_mwh, price_base_year FROM desnz_price_scenarios WHERE vintage = ? ORDER BY scenario, year",
        (vintage,),
    ).fetchall()
    out: dict[str, list[dict]] = {s: [] for s in SCENARIOS}
    for scenario, year, value, price_base_year in rows:
        deflated = _deflate(value, price_base_year, to_year, index)
        if deflated is not None:
            out.setdefault(scenario, []).append({"year": year, "value": deflated})
    return out


def _chart_data(outturn: list[dict], scenarios: dict) -> dict:
    """Shapes outturn_by_year()/scenarios_for_vintage()'s output for
    Chart.js -- one shared array of calendar-year labels, contiguous
    from the earliest year any series has to the latest (same
    "don't compress real gaps" reasoning as ppa_metrics._month_range()),
    with each series' own values placed at their matching year and
    `None` elsewhere (Chart.js's spanGaps:false breaks the line there,
    not a fabricated interpolation).
    """
    years = {p["year"] for p in outturn}
    for series in scenarios.values():
        years |= {p["year"] for p in series}
    if not years:
        return {"labels": [], "outturn": [], "reference": [], "ffp_low": [], "ffp_high": []}

    labels = list(range(min(years), max(years) + 1))
    outturn_by_year = {p["year"]: p["value"] for p in outturn}
    scenario_by_year = {s: {p["year"]: p["value"] for p in scenarios.get(s, [])} for s in SCENARIOS}
    return {
        "labels": labels,
        "outturn": [outturn_by_year.get(y) for y in labels],
        "reference": [scenario_by_year["reference"].get(y) for y in labels],
        "ffp_low": [scenario_by_year["ffp_low"].get(y) for y in labels],
        "ffp_high": [scenario_by_year["ffp_high"].get(y) for y in labels],
    }


def long_term_outlook(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Everything the PPA Tools "Long-term price outlook" card needs.
    Always shows the latest ingested DESNZ vintage (see
    available_vintages() for the full list, kept for a future
    vintage-picker -- not built in v1).
    """
    today = today or date.today()
    index = _deflator_index(conn)
    money_year = _resolve_money_year(index, today)

    vintages = available_vintages(conn)
    latest_vintage = vintages[-1] if vintages else None
    scenarios = scenarios_for_vintage(conn, latest_vintage, money_year, index) if latest_vintage else {s: [] for s in SCENARIOS}
    outturn = outturn_by_year(conn, money_year, index)

    return {
        "chart_data": _chart_data(outturn, scenarios),
        "money_year": money_year,
        "money_year_clamped": money_year != today.year,
        "desnz_base_year": DESNZ_BASE_YEAR,
        "vintage": latest_vintage,
        "other_vintages": [v for v in vintages if v != latest_vintage],
        "has_data": bool(index) and latest_vintage is not None,
    }
