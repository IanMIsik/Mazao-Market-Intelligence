"""
metrics.build_week(conn, week_ending) -> dict

Reads one week of stored data and emits a plain-data "facts dict" (JSON
serialisable) with exactly the figures the reference sketch displays.
No SVG/pixel geometry here -- that's render's job. No narrative text here --
that's a future stage; render fills the headline/driver copy with simple
templated sentences built directly from these numbers.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta

from .settlement import periods_in_date, week_dates
from .storage import series_for_week

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PRICE_THRESHOLD = 100.0
MW_PER_GW = 1000.0

REQUIRED_SERIES = ("day_ahead", "imbalance", "wind", "total_generation", "demand")

# imbalance/wind/total_generation/demand gaps almost always mean a real
# ingest problem and stay fatal. day_ahead is handled separately below: a
# day_ahead gap usually means Elexon's MID providers (APXMIDP/N2EXMIDP)
# genuinely recorded no priced trade for that half-hour -- confirmed live
# against the real API (both providers report price=0, volume=0 for the
# missing periods) rather than assumed. Re-fetching that period will never
# produce data that was never generated, so refusing to render the whole
# week over it is unhelpful. A day_ahead day with SOME periods still
# computes a real (if partial) average/peak/spread -- build_week()'s
# per-day loop already derives its period set from what's actually present,
# not an assumed fixed count. A day with ZERO day_ahead periods is the one
# exception that still blocks -- there's no average to compute at all.
STRICT_SERIES = ("imbalance", "wind", "total_generation", "demand")


class IncompleteWeekError(Exception):
    pass


def _load_series(conn: sqlite3.Connection, series: str, dates: list[date]) -> dict[tuple[str, int], float]:
    return series_for_week(conn, series, dates)


def _check_completeness(loaded: dict[str, dict[tuple[str, int], float]], dates: list[date]) -> list[dict]:
    """Raises IncompleteWeekError for any STRICT_SERIES gap, or a day with
    zero day_ahead periods. Returns the list of day_ahead gaps that were
    tolerated (empty on a fully complete week) so callers can disclose them
    rather than silently averaging around a partial day.
    """
    problems = []
    day_ahead_gaps = []
    for d in dates:
        expected = periods_in_date(d)
        for series in STRICT_SERIES:
            actual = sum(1 for (sd, _sp) in loaded[series] if sd == d.isoformat())
            if actual != expected:
                problems.append(f"{series} on {d.isoformat()}: expected {expected} periods, have {actual}")

        da_periods = sorted(sp for (sd, sp) in loaded["day_ahead"] if sd == d.isoformat())
        if not da_periods:
            problems.append(f"day_ahead on {d.isoformat()}: expected {expected} periods, have 0")
        elif len(da_periods) != expected:
            missing = sorted(set(range(1, expected + 1)) - set(da_periods))
            day_ahead_gaps.append({"date": d.isoformat(), "expected": expected, "missing_periods": missing})

    if problems:
        raise IncompleteWeekError(
            "Week is missing settlement periods, refusing to render:\n  " + "\n  ".join(problems)
        )
    return day_ahead_gaps


def _best_1h_spread(values: list[float]) -> float:
    """Mean of the two highest periods less the two lowest, in £/MWh.

    No cycling limits, degradation or round-trip losses. `values` must have
    at least 4 settlement periods (always true -- shortest settlement day
    has 46).
    """
    ordered = sorted(values)
    lowest_two = ordered[:2]
    highest_two = ordered[-2:]
    return statistics.mean(highest_two) - statistics.mean(lowest_two)


def _day_label(d: date) -> str:
    return f"{DAY_NAMES[d.weekday()]} {d.day} {d.strftime('%b')}"


def _short_label(d: date) -> str:
    return f"{DAY_NAMES[d.weekday()]} {d.day}"


def _week_avg(day_ahead: dict[tuple[str, int], float], dates: list[date]) -> float | None:
    vals = [v for (sd, _sp), v in day_ahead.items() if sd in {d.isoformat() for d in dates}]
    return round(statistics.mean(vals), 2) if vals else None


def _week_wind_share(wind: dict, total_gen: dict, dates: list[date]) -> float | None:
    sd_set = {d.isoformat() for d in dates}
    wind_sum = sum(v for (sd, _sp), v in wind.items() if sd in sd_set)
    total_sum = sum(v for (sd, _sp), v in total_gen.items() if sd in sd_set)
    if total_sum <= 0:
        return None
    return round(100.0 * wind_sum / total_sum, 1)


def build_week(conn: sqlite3.Connection, week_ending: date) -> dict:
    dates = week_dates(week_ending)
    prior_dates = [d - timedelta(days=7) for d in dates]
    trailing_30_dates = [dates[0] - timedelta(days=n) for n in range(1, 31)]

    loaded = {s: _load_series(conn, s, dates) for s in REQUIRED_SERIES}
    day_ahead_gaps = _check_completeness(loaded, dates)

    day_ahead, imbalance, wind, total_gen, demand = (
        loaded["day_ahead"], loaded["imbalance"], loaded["wind"], loaded["total_generation"], loaded["demand"]
    )

    days_out = []
    heatmap = []
    half_hourly_da = []
    half_hourly_imb = []
    week_periods_above_100 = 0

    for d in dates:
        sd = d.isoformat()
        sps = sorted(sp for (s, sp) in day_ahead if s == sd)
        da_vals = [day_ahead[(sd, sp)] for sp in sps]
        day_mean = statistics.mean(da_vals)

        day_label = _day_label(d)
        for sp in sps:
            half_hourly_da.append({"sd": sd, "sp": sp, "value": day_ahead[(sd, sp)], "day_label": day_label})
            heatmap.append({
                "sd": sd, "sp": sp, "day_label": DAY_NAMES[d.weekday()],
                "delta": round(day_ahead[(sd, sp)] - day_mean, 0),
            })

        imb_sps = sorted(sp for (s, sp) in imbalance if s == sd)
        for sp in imb_sps:
            half_hourly_imb.append({"sd": sd, "sp": sp, "value": imbalance[(sd, sp)], "day_label": day_label})

        wind_vals = [wind[(sd, sp)] / MW_PER_GW for sp in sps if (sd, sp) in wind]
        demand_vals = [demand[(sd, sp)] / MW_PER_GW for sp in sps if (sd, sp) in demand]
        wind_share_day = _week_wind_share(
            {k: v for k, v in wind.items() if k[0] == sd},
            {k: v for k, v in total_gen.items() if k[0] == sd},
            [d],
        )
        periods_above_100 = sum(1 for v in da_vals if v > PRICE_THRESHOLD)
        week_periods_above_100 += periods_above_100

        days_out.append({
            "date": sd,
            "label": _day_label(d),
            "short_label": _short_label(d),
            "dow_letter": DAY_NAMES[d.weekday()][0],
            "periods": len(sps),
            "day_ahead_avg": round(day_mean, 2),
            "day_ahead_peak": round(max(da_vals), 2),
            "day_ahead_trough": round(min(da_vals), 2),
            "best_spread": round(_best_1h_spread(da_vals), 2),
            "wind_share_pct": wind_share_day,
            "wind_avg_gw": round(statistics.mean(wind_vals), 1) if wind_vals else None,
            "demand_peak_gw": round(max(demand_vals), 1) if demand_vals else None,
            "periods_above_100": periods_above_100,
        })

    best_spread_day = max(days_out, key=lambda r: r["best_spread"])
    highest_imb = max(half_hourly_imb, key=lambda r: r["value"])
    highest_imb_date = date.fromisoformat(highest_imb["sd"])

    avg_day_ahead = _week_avg(day_ahead, dates)
    wind_share_week = _week_wind_share(wind, total_gen, dates)

    prior_day_ahead = _load_series(conn, "day_ahead", prior_dates)
    prior_avg = _week_avg(prior_day_ahead, prior_dates) if len(prior_day_ahead) >= 46 * 7 else None
    avg_pct_change = round(100.0 * (avg_day_ahead - prior_avg) / prior_avg, 1) if prior_avg else None

    prior_wind = _load_series(conn, "wind", prior_dates)
    prior_total_gen = _load_series(conn, "total_generation", prior_dates)
    prior_wind_share = _week_wind_share(prior_wind, prior_total_gen, prior_dates) if prior_wind else None
    wind_share_pct_change = round(wind_share_week - prior_wind_share, 0) if (wind_share_week and prior_wind_share) else None

    trailing_da = _load_series(conn, "day_ahead", trailing_30_dates)
    trailing_by_day: dict[str, list[float]] = {}
    for (sd, _sp), v in trailing_da.items():
        trailing_by_day.setdefault(sd, []).append(v)
    trailing_spreads = [_best_1h_spread(vs) for vs in trailing_by_day.values() if len(vs) >= 4]
    spread_30d_median = round(statistics.median(trailing_spreads), 2) if len(trailing_spreads) >= 20 else None

    return {
        "week_ending": week_ending.isoformat(),
        "week_start": dates[0].isoformat(),
        "days": days_out,
        "half_hourly": {"day_ahead": half_hourly_da, "imbalance": half_hourly_imb},
        "heatmap": heatmap,
        "kpi": {
            "avg_day_ahead": avg_day_ahead,
            "avg_day_ahead_pct_change": avg_pct_change,
            "best_spread_week": best_spread_day["best_spread"],
            "best_spread_week_day": best_spread_day["label"],
            "spread_30d_median": spread_30d_median,
            "wind_share_pct": round(wind_share_week) if wind_share_week is not None else None,
            "wind_share_pct_change_pts": wind_share_pct_change,
            "highest_imbalance_value": round(highest_imb["value"], 0),
            "highest_imbalance_day_label": _day_label(highest_imb_date),
            "highest_imbalance_sp": highest_imb["sp"],
        },
        "totals": {"periods_above_100_week": week_periods_above_100},
        "drivers": {
            "wind": {
                "low_gw": min(days_out, key=lambda r: r["wind_avg_gw"])["wind_avg_gw"],
                "low_day": min(days_out, key=lambda r: r["wind_avg_gw"])["label"],
                "high_gw": max(days_out, key=lambda r: r["wind_avg_gw"])["wind_avg_gw"],
                "high_day": max(days_out, key=lambda r: r["wind_avg_gw"])["label"],
            },
            "demand": {
                "peak_gw": max(days_out, key=lambda r: r["demand_peak_gw"])["demand_peak_gw"],
                "peak_day": max(days_out, key=lambda r: r["demand_peak_gw"])["label"],
            },
            "periods_above_100": {
                "high": max(days_out, key=lambda r: r["periods_above_100"])["periods_above_100"],
                "high_day": max(days_out, key=lambda r: r["periods_above_100"])["label"],
                "low": min(days_out, key=lambda r: r["periods_above_100"])["periods_above_100"],
                "low_day": min(days_out, key=lambda r: r["periods_above_100"])["label"],
            },
        },
        "run_basis": (
            "Elexon's system-prices endpoint does not expose a settlement-run identifier and always "
            "returns its latest available run per period; for a report built this soon after week end "
            "that is effectively the initial run, ahead of later reconciliation."
        ),
        "day_ahead_gaps": day_ahead_gaps,
    }
