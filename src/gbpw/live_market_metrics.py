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


def actual_plus_addon_vs_forecast(
    conn: sqlite3.Connection, actual_series: str, addon_series: str, forecast_series: str, today: date
) -> dict:
    """Same shape as actual_vs_forecast(), but "actual" at each settlement
    period is actual_series + addon_series -- for a metric where the raw
    reading understates the true figure and a separately-ingested addon
    corrects it (wind + wind_curtailed_mw: FUELHH's wind generation alone
    doesn't show capacity that was instructed off via the Balancing
    Mechanism, see ingest/wind_curtailment.py).

    A period the addon hasn't reached yet shows as missing (actual=None),
    not silently treated as zero -- wind_curtailed_mw specifically lags
    real time by ~18 minutes (Elexon's own bid-acceptance publish delay,
    see settlement.bid_data_published()), so "no addon row yet" means
    "not published yet," never "confirmed zero curtailment." This is why
    this combined view is kept as its own chart, separate from the plain
    actual_vs_forecast() line for the same base series -- the two update
    on genuinely different cadences, and merging them into one line would
    make the lag look like a data dropout in the fast-updating series.
    """
    actual = today_progression(conn, actual_series, today)
    addon_loaded = series_for_week(conn, addon_series, [today])
    addon_by_sp = {sp: v for (_sd, sp), v in addon_loaded.items()}
    forecast_loaded = series_for_week(conn, forecast_series, [today])
    forecast_by_sp = {sp: v for (_sd, sp), v in forecast_loaded.items()}

    seen_sps = {p["sp"] for p in actual["points"]}
    points = [
        {
            "sp": p["sp"],
            "actual": round(p["value"] + addon_by_sp[p["sp"]], 2) if p["sp"] in addon_by_sp else None,
            "forecast": round(forecast_by_sp[p["sp"]], 2) if p["sp"] in forecast_by_sp else None,
        }
        for p in actual["points"]
    ]
    for sp, v in sorted(forecast_by_sp.items()):
        if sp not in seen_sps:
            points.append({"sp": sp, "actual": None, "forecast": round(v, 2)})
    points.sort(key=lambda p: p["sp"])

    # "Latest" here means the latest period this combined figure can
    # actually speak to -- the rightmost point with a real (non-None)
    # actual -- not actual_series' own latest_sp, which will usually be
    # ahead of it by however many periods the publish lag covers.
    latest_sp = None
    latest_actual = None
    for p in reversed(points):
        if p["actual"] is not None:
            latest_sp, latest_actual = p["sp"], p["actual"]
            break
    latest_forecast = forecast_by_sp.get(latest_sp) if latest_sp is not None else None
    return {
        "latest_actual": latest_actual,
        "latest_forecast": round(latest_forecast, 2) if latest_forecast is not None else None,
        "latest_sp": latest_sp,
        "points": points,
    }


def actual_and_addon_vs_forecast(
    conn: sqlite3.Connection, actual_series: str, addon_series: str, forecast_series: str, today: date
) -> dict:
    """One merged set of points for a single chart showing all three:
    raw actual, actual+addon ("combined"), and forecast -- e.g. wind vs
    wind+curtailed vs WINDFOR, so both the fast-updating metered reading
    and the slower, curtailment-corrected figure are visible on the same
    axes without needing two separate cards.

    `combined` is None for any period the addon hasn't published yet
    (same lag-aware honesty as actual_plus_addon_vs_forecast() -- see
    that function's docstring) even when `actual` itself has a real
    value, so the two solid lines will genuinely end at different
    settlement periods on a live, partial day. That's real, not a bug --
    the chart (charts_live.triple_comparison_svg()) draws each its own
    end marker rather than implying one broken series.
    """
    actual = today_progression(conn, actual_series, today)
    addon_loaded = series_for_week(conn, addon_series, [today])
    addon_by_sp = {sp: v for (_sd, sp), v in addon_loaded.items()}
    forecast_loaded = series_for_week(conn, forecast_series, [today])
    forecast_by_sp = {sp: v for (_sd, sp), v in forecast_loaded.items()}

    seen_sps = {p["sp"] for p in actual["points"]}
    points = [
        {
            "sp": p["sp"],
            "actual": p["value"],
            "combined": round(p["value"] + addon_by_sp[p["sp"]], 2) if p["sp"] in addon_by_sp else None,
            "forecast": round(forecast_by_sp[p["sp"]], 2) if p["sp"] in forecast_by_sp else None,
        }
        for p in actual["points"]
    ]
    for sp, v in sorted(forecast_by_sp.items()):
        if sp not in seen_sps:
            points.append({"sp": sp, "actual": None, "combined": None, "forecast": round(v, 2)})
    points.sort(key=lambda p: p["sp"])

    return {"points": points}


def deviation_regions(
    points: list[dict], floor_mw: float = 20.0, pct_threshold: float = 0.10, min_run: int = 2
) -> list[dict]:
    """Settlement-period runs where an interconnector's actual flow
    sustained-deviates from its scheduled flow, for highlighting on the
    Interconnectors tab's actual-vs-scheduled chart. Takes the same
    `points` shape actual_vs_forecast() returns.

    A real, scale-aware threshold, not "any nonzero gap": live flows
    checked while designing this showed normal operational noise of its
    own, scaling with the link's own flow (e.g. North Sea Link ~3-5% of
    a 700-1000 MW schedule, IFA ~2-5% of a 200-600 MW schedule) -- a
    naive "any difference" flag would fire on nearly every settlement
    period for some links. `floor_mw` (default 20 MW) matters
    specifically when scheduled flow is at or near zero -- a pure
    percentage threshold blows up there (seen live: IFA scheduled=0,
    actual=10 -- not a real deviation, just a small blip against nothing).
    A run only counts once it holds for `min_run` (default 2, i.e. one
    hour) consecutive periods in the same direction -- a single noisy
    sample isn't a sustained trend worth flagging. Either side missing
    (not yet published) breaks a run rather than being treated as zero
    deviation.
    """
    def direction_and_diff(actual: float | None, scheduled: float | None) -> tuple[str | None, float | None]:
        if actual is None or scheduled is None:
            return None, None
        diff = actual - scheduled
        threshold = max(floor_mw, pct_threshold * abs(scheduled))
        if abs(diff) <= threshold:
            return None, None
        return ("over" if diff > 0 else "under"), diff

    regions: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        if len(run) >= min_run:
            avg_diff = sum(r["diff"] for r in run) / len(run)
            regions.append({
                "start_sp": run[0]["sp"],
                "end_sp": run[-1]["sp"],
                "direction": run[0]["direction"],
                # The run's own average gap, not each point's individual one --
                # a single headline figure for the label, and averaging (rather
                # than peak) keeps a single outlier period from overstating the
                # whole sustained run.
                "avg_deviation_mw": round(abs(avg_diff), 1),
            })
        run.clear()

    for p in points:
        d, diff = direction_and_diff(p["actual"], p["forecast"])
        if d is None:
            flush()
            continue
        if run and run[-1]["direction"] != d:
            flush()
        run.append({"sp": p["sp"], "direction": d, "diff": diff})
    flush()
    return regions


def dual_series_today(conn: sqlite3.Connection, series_a: str, series_b: str, today: date) -> dict:
    """Today's two progressions, paired by settlement period, for a chart
    with two independent y-axes (charts_live.dual_series_svg()) -- for two
    series with genuinely different units where one shared scale would be
    meaningless (e.g. imbalance price £/MWh vs imbalance volume MWh; a
    "150" on one is not comparable to a "150" on the other). Unlike
    actual_vs_forecast(), neither series is secondary here -- both are
    independently live actuals -- so a point exists for any settlement
    period either series has, with the other side None if that series
    hasn't cleared for it.
    """
    a = today_progression(conn, series_a, today)
    b = today_progression(conn, series_b, today)
    a_by_sp = {p["sp"]: p["value"] for p in a["points"]}
    b_by_sp = {p["sp"]: p["value"] for p in b["points"]}
    all_sps = sorted(set(a_by_sp) | set(b_by_sp))
    return {"points": [{"sp": sp, "a": a_by_sp.get(sp), "b": b_by_sp.get(sp)} for sp in all_sps]}
