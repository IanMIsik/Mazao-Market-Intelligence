"""
Data layer for the Live Market page -- daily stats for the in-progress week
(Monday through yesterday) plus today's live, partial progression. Pure
functions, no web-framework imports, same convention as eac_metrics.py /
bm_metrics.py.

Written generically over `series` rather than one function per dataset:
this page ends up showing day-ahead, imbalance, wind, demand, and (later
phases) solar, ITSDO, forecasts and interconnector flows, all through the
same "daily avg/peak/trough" and "today's progression" shapes. One
generic implementation here instead of copy-pasting metrics.py's per-day
loop for every new dataset.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from .settlement import most_recent_sunday
from .storage import series_for_week


def week_so_far(today: date) -> tuple[date, date] | None:
    """Monday of the in-progress week through yesterday. None if today is
    itself Monday -- the week has no complete trailing days yet, only
    today's live progression is meaningful.
    """
    monday = most_recent_sunday(today) + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    if monday > yesterday:
        return None
    return monday, yesterday


def day_stats(conn: sqlite3.Connection, series: str, dates: list[date]) -> list[dict]:
    """One entry per date: {date, avg, peak, trough, periods}. `periods` is
    however many settlement periods actually have data for that date --
    never assumed to be 46/48/50 -- so a genuinely partial or missing day
    shows as such (avg/peak/trough None, periods 0) rather than being
    silently skipped or crashing on an empty mean.
    """
    if not dates:
        return []
    loaded = series_for_week(conn, series, dates)
    out = []
    for d in dates:
        sd = d.isoformat()
        vals = [v for (s, _sp), v in loaded.items() if s == sd]
        if vals:
            out.append({
                "date": sd, "avg": round(statistics.mean(vals), 2),
                "peak": round(max(vals), 2), "trough": round(min(vals), 2),
                "periods": len(vals),
            })
        else:
            out.append({"date": sd, "avg": None, "peak": None, "trough": None, "periods": 0})
    return out


def today_progression(conn: sqlite3.Connection, series: str, today: date) -> dict:
    """Today's settlement-period values so far, for a live-updating chart,
    plus the latest single value for a KPI card. Empty/None-latest is a
    real, expected state early in the day -- not an error.
    """
    loaded = series_for_week(conn, series, [today])
    points = sorted(((sp, v) for (_sd, sp), v in loaded.items()), key=lambda p: p[0])
    if not points:
        return {"latest_value": None, "latest_sp": None, "points": []}
    latest_sp, latest_value = points[-1]
    return {
        "latest_value": round(latest_value, 2),
        "latest_sp": latest_sp,
        "points": [{"sp": sp, "value": round(v, 2)} for sp, v in points],
    }


def delta_vs_yesterday(conn: sqlite3.Connection, series: str, today: date) -> float | None:
    """Today's latest value minus yesterday's value at the *same*
    settlement period -- the most comparable prior figure for a KPI delta.
    None if either side is missing (early in the day, or yesterday genuinely
    has a gap) rather than comparing against a different period.
    """
    today_data = today_progression(conn, series, today)
    if today_data["latest_value"] is None:
        return None
    yesterday = today - timedelta(days=1)
    loaded = series_for_week(conn, series, [yesterday])
    prior = loaded.get((yesterday.isoformat(), today_data["latest_sp"]))
    if prior is None:
        return None
    return round(today_data["latest_value"] - prior, 2)


def actual_vs_forecast(conn: sqlite3.Connection, actual_series: str, forecast_series: str, today: date) -> dict:
    """Today's actual progression paired with the latest-published forecast
    for the same settlement periods (series_for_week()'s MAX(run)
    resolution already picks the latest vintage -- see storage.py). Also
    includes forecast-only periods later today that haven't cleared as
    actuals yet, so the chart can show the forecast running ahead of the
    actual line. A period missing one side shows None for it, never a
    fabricated value.
    """
    actual = today_progression(conn, actual_series, today)
    forecast_loaded = series_for_week(conn, forecast_series, [today])
    forecast_by_sp = {sp: v for (_sd, sp), v in forecast_loaded.items()}

    seen_sps = {p["sp"] for p in actual["points"]}
    points = [
        {"sp": p["sp"], "actual": p["value"],
         "forecast": round(forecast_by_sp[p["sp"]], 2) if p["sp"] in forecast_by_sp else None}
        for p in actual["points"]
    ]
    for sp, v in sorted(forecast_by_sp.items()):
        if sp not in seen_sps:
            points.append({"sp": sp, "actual": None, "forecast": round(v, 2)})
    points.sort(key=lambda p: p["sp"])

    latest_forecast = forecast_by_sp.get(actual["latest_sp"]) if actual["latest_sp"] is not None else None
    return {
        "latest_actual": actual["latest_value"],
        "latest_forecast": round(latest_forecast, 2) if latest_forecast is not None else None,
        "latest_sp": actual["latest_sp"],
        "points": points,
    }
