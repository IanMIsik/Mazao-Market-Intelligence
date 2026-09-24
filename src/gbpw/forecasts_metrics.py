"""
Data layer for the Forecasts page -- wind, demand and solar across a
14-day rolling window (today+1..today+14), each stitched from whichever
source actually covers that day:

- Wind/demand day 1: Elexon's own WINDFOR/NDF, fetched a day ahead
  specifically for this page (see ingest/elexon.py's
  fetch_wind_forecast_tomorrow()/fetch_demand_forecast_tomorrow()) --
  Live Market's own day-ahead ingest never asked for tomorrow before.
  Demand specifically falls back to "demand_forecast_14d" for day 1
  whenever Elexon's NDF isn't yet fully published for that date (see
  _day1_demand_series()) -- NDF arrives incrementally through the day,
  not as one complete batch, and demand_forecast_14d's own rolling
  window happens to still cover what's now day 1 (real, already-ingested
  data, not a new source), so it stands in until NDF catches up.
- Wind/demand days 2-14: NESO's own purpose-built medium-term products
  (series "wind_forecast_14d"/"demand_forecast_14d"), which don't reach
  back to day 1 at all -- confirmed live, the demand one genuinely starts
  at day+2, not day+1 -- except demand's own day-1 fallback above.
- Solar, the whole window: already one continuous series
  ("solar_forecast", the embedded wind/solar forecast Live Market also
  uses) -- NESO's own rolling window already spans day 0-14, so there's
  no day-1/days-2-14 split to make here the way wind/demand need.
- Nuclear, days 2-14 only: Elexon's own generation availability forecast
  (series "nuclear_forecast_14d", see ingest/elexon.py's
  fetch_nuclear_forecast_medium()) -- one value per calendar day (this
  dataset's real granularity, nuclear output barely varies intraday),
  broadcast across every settlement period of its day so it joins
  against the per-period series the same way. Never covers day 1 at all,
  and isn't backfilled -- residual_demand() subtracts it only when
  available rather than requiring it the way wind/solar are required.

Pure functions, no web-framework imports, same convention as
live_market_metrics.py -- reuses its today_progression() for the
single-day query shape rather than duplicating it.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .live_market_metrics import today_progression
from .settlement import periods_in_date


def forecast_window_days(today: date) -> list[date]:
    """The 14 calendar dates this page covers -- tomorrow through 14 days
    out. Deterministic, not a DB query: the window is always exactly this
    shape regardless of how much of it has actually been ingested yet.
    """
    return [today + timedelta(days=i) for i in range(1, 15)]


def _stitch(conn: sqlite3.Connection, day_1_series: str, days_2_14_series: str, today: date) -> list[dict]:
    """Every point across the full 14-day window, in order, each carrying
    its own {date, sp, value} -- day 1 from `day_1_series`, days 2-14 from
    `days_2_14_series`. A day with nothing ingested yet simply contributes
    no points (today_progression() never fabricates a value), not a
    fabricated gap-filled entry.
    """
    points: list[dict] = []
    for i, d in enumerate(forecast_window_days(today)):
        series = day_1_series if i == 0 else days_2_14_series
        for p in today_progression(conn, series, d)["points"]:
            points.append({"date": d.isoformat(), "sp": p["sp"], "value": p["value"]})
    return points


def wind_forecast(conn: sqlite3.Connection, today: date, selected_day: date | None = None) -> list[dict]:
    """selected_day=None returns the full 14-day window (see _stitch()).
    A specific date narrows to just that day's own {sp, value} points --
    same shape progression_svg() already renders, so a single-day view
    reuses that renderer unchanged rather than needing its own.
    """
    if selected_day is not None:
        series = "wind_forecast" if selected_day == forecast_window_days(today)[0] else "wind_forecast_14d"
        return today_progression(conn, series, selected_day)["points"]
    return _stitch(conn, "wind_forecast", "wind_forecast_14d", today)


def _day1_demand_series(conn: sqlite3.Connection, day1: date) -> str:
    """Which series day 1's demand should actually come from -- Elexon's
    own day-ahead NDF ("demand_forecast") when it's fully published for
    that date, "demand_forecast_14d" (NESO's medium-term product,
    normally days 2-14) otherwise. NDF is published incrementally through
    the day, not as one complete 48-period batch, so a page view that
    lands before NDF has caught up would otherwise show a demand chart
    that visibly stops partway through the day. demand_forecast_14d can
    stand in for day 1 specifically because it's a *rolling* window kept
    fresh by background_refresh's 5-minute cycle -- confirmed live, what
    was "day 2" in yesterday's fetch is still sitting in the database
    (upsert is additive, never deleted) and is now day 1's own date, with
    full period coverage, simply because a day has passed since it was
    fetched. Not a new data source, just a completeness check on one
    that's already there. Switches back to Elexon the moment NDF's own
    count for that date reaches a full day -- direct request: prefer
    Elexon, only fall back while it's genuinely incomplete.
    """
    elexon_periods = conn.execute(
        "SELECT COUNT(DISTINCT sp) FROM prices WHERE series='demand_forecast' AND sd=?", (day1.isoformat(),)
    ).fetchone()[0]
    return "demand_forecast" if elexon_periods >= periods_in_date(day1) else "demand_forecast_14d"


def demand_forecast(conn: sqlite3.Connection, today: date, selected_day: date | None = None) -> list[dict]:
    day1 = forecast_window_days(today)[0]
    if selected_day is not None:
        series = _day1_demand_series(conn, day1) if selected_day == day1 else "demand_forecast_14d"
        return today_progression(conn, series, selected_day)["points"]
    return _stitch(conn, _day1_demand_series(conn, day1), "demand_forecast_14d", today)


def solar_forecast(conn: sqlite3.Connection, today: date, selected_day: date | None = None) -> list[dict]:
    """Solar has no day-1/days-2-14 split to make -- "solar_forecast"
    (the embedded wind/solar forecast) already covers the whole window in
    one series, so a single day is just today_progression() for that date
    and "all" is that same series read across every day in the window.
    """
    if selected_day is not None:
        return today_progression(conn, "solar_forecast", selected_day)["points"]
    points: list[dict] = []
    for d in forecast_window_days(today):
        for p in today_progression(conn, "solar_forecast", d)["points"]:
            points.append({"date": d.isoformat(), "sp": p["sp"], "value": p["value"]})
    return points


def nuclear_forecast(conn: sqlite3.Connection, today: date, selected_day: date | None = None) -> list[dict]:
    """Nuclear availability, broadcast across every settlement period of
    its day at ingest time (see elexon.fetch_nuclear_forecast_medium()'s
    own docstring for why that's a deliberate, disclosed choice, not a
    fabrication). Only ever covers days 2-14 -- this Elexon dataset
    genuinely doesn't reach day 1, and isn't backfilled from a separate
    day-ahead product the way wind/demand's own day-1 gap is. A selected
    day of 1 (or "all", which includes day 1) correctly comes back with no
    nuclear points for that day -- residual_demand() treats that as
    "nothing to subtract here", not zero.
    """
    if selected_day is not None:
        return today_progression(conn, "nuclear_forecast_14d", selected_day)["points"]
    points: list[dict] = []
    for d in forecast_window_days(today):
        for p in today_progression(conn, "nuclear_forecast_14d", d)["points"]:
            points.append({"date": d.isoformat(), "sp": p["sp"], "value": p["value"]})
    return points


def nuclear_forecast_by_day(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Nuclear availability as one number per day, for display -- the raw
    series already repeats the same value across all 48 periods of a day
    (see fetch_nuclear_forecast_medium()), so this just reads one
    representative period (SP1) back out per day rather than a genuine
    per-period series. Day 1 is never included (see nuclear_forecast()'s
    own docstring) -- the returned list is only ever as long as however
    many of days 2-14 have actually been ingested.
    """
    out: list[dict] = []
    for d in forecast_window_days(today):
        points = today_progression(conn, "nuclear_forecast_14d", d)["points"]
        if points:
            # A real date object, not the isoformat string the chart-point
            # functions use -- this is display-only (never serialized to
            # JSON for a chart), so the template can format it the same
            # way it already formats the day-picker's own dates.
            out.append({"date": d, "value": points[0]["value"]})
    return out


def residual_demand(conn: sqlite3.Connection, today: date, selected_day: date | None = None) -> list[dict]:
    """Demand minus wind minus nuclear -- solar deliberately not included
    (direct request), so this is the load left for everything else once
    the two largest non-dispatchable/must-run sources are accounted for,
    not literally "everything except demand". Wind is a hard requirement:
    a period missing it is left out of the result entirely, never a
    partial subtraction against a missing input. Nuclear is subtracted
    *when available*, not required -- it only ever covers days 2-14 (see
    nuclear_forecast()), and requiring it too would blank out day 1's
    residual demand entirely, which is worse than the alternative of
    disclosing the day-1/days-2-14 boundary explicitly (see
    forecasts.html's own note) rather than hiding it behind an empty
    chart for the single day view most likely to actually be looked at.
    """
    demand_pts = demand_forecast(conn, today, selected_day)
    wind_by_key = {(p.get("date"), p["sp"]): p["value"] for p in wind_forecast(conn, today, selected_day)}
    nuclear_by_key = {(p.get("date"), p["sp"]): p["value"] for p in nuclear_forecast(conn, today, selected_day)}

    out: list[dict] = []
    for p in demand_pts:
        k = (p.get("date"), p["sp"])
        if k not in wind_by_key:
            continue
        value = p["value"] - wind_by_key[k]
        if k in nuclear_by_key:
            value -= nuclear_by_key[k]
        row = {"sp": p["sp"], "value": round(value, 2)}
        if "date" in p:
            row["date"] = p["date"]
        out.append(row)
    return out


def stat_summary(points: list[dict]) -> dict:
    """max/min/mean of exactly the points passed in -- always matches
    whatever's currently plotted (a specific day or the full window),
    never a fixed separate window of its own.
    """
    if not points:
        return {"max": None, "min": None, "mean": None}
    values = [p["value"] for p in points]
    return {"max": round(max(values), 1), "min": round(min(values), 1), "mean": round(sum(values) / len(values), 1)}


def full_window_available(conn: sqlite3.Connection, series_fn, today: date) -> bool:
    """Whether every day in the 14-day window has a genuinely complete
    settlement day's worth of periods (46/48/50, DST-aware) for this
    forecast -- not just "some points". Checks real coverage, not
    plausibility: a day can be complete and still carry a suspicious flat
    reading (confirmed live for wind from a few days out) -- that's a
    real, separate observation this deliberately does not try to detect,
    since distinguishing "a genuinely calm day" from "the source hasn't
    updated that far out yet" isn't something a period count can tell.
    """
    points = series_fn(conn, today)
    counts: dict[str, int] = {}
    for p in points:
        counts[p["date"]] = counts.get(p["date"], 0) + 1
    return all(counts.get(d.isoformat(), 0) >= periods_in_date(d) for d in forecast_window_days(today))
