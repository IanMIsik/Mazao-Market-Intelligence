"""
Forecasts: wind, demand and solar across the next 14 days (see
forecasts_metrics.py for exactly which NESO/Elexon products cover which
part of that window), plus a Battery Optimization tab -- a placeholder
until the user's existing tool (built elsewhere) is ported in here.

The day filter is one shared `day` query param, not one per chart --
same "pick one value, the whole page recomputes together" pattern PPA
Tools' year filter already uses (a GET link, not a client-side toggle),
not three independent filters that could disagree with each other.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import forecasts_metrics as fm
from ..storage import latest_fetch_ts
from . import charts_live, http_cache
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"

CHARTS = [
    # show_min=False for solar only -- its minimum is trivially ~0 every
    # single night, never an interesting or varying figure, unlike
    # wind/demand/residual's real troughs (residual's own minimum can be
    # negative on a windy/sunny/low-nuclear-demand day, which is exactly
    # the informative case worth surfacing, not hiding).
    {"key": "wind", "label": "Wind", "unit": "MW", "color": "var(--lm-teal)", "fn": fm.wind_forecast, "show_min": True},
    {"key": "demand", "label": "Demand", "unit": "MW", "color": "var(--lm-violet)", "fn": fm.demand_forecast, "show_min": True},
    {"key": "solar", "label": "Solar", "unit": "MW", "color": "var(--lm-gold)", "fn": fm.solar_forecast, "show_min": False},
    {"key": "residual", "label": "Residual demand", "unit": "MW", "color": "var(--lm-amber)", "fn": fm.residual_demand, "show_min": True},
]


def _parse_day(day: str, valid_days: list[date]) -> date | None:
    if day == "all":
        return None
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    return parsed if parsed in valid_days else None


@router.get("/forecasts", response_class=HTMLResponse)
def forecasts_page(request: Request, day: str = "all", db: sqlite3.Connection = Depends(get_db)):
    today = date.today()
    days = fm.forecast_window_days(today)
    # An unrecognized/invalid day falls back to "all" rather than 404ing
    # or silently rendering an empty chart -- same forgiving-default
    # convention PPA Tools' window picker uses.
    selected_day = _parse_day(day, days)

    # "All 14 days" is only offered (and only ever actually shown) once
    # wind AND solar genuinely have every period of every day -- direct
    # request: don't offer a mode that would visibly stop partway through
    # the window. A day that hasn't ingested yet falls back to day 1 (a
    # real, complete day) rather than an incomplete "all" view -- this
    # runs before the day is finalised so that fallback is reflected in
    # both the rendered charts and the ETag below.
    all_available = fm.full_window_available(db, fm.wind_forecast, today) and fm.full_window_available(
        db, fm.solar_forecast, today
    )
    if selected_day is None and not all_available:
        selected_day = days[0]
    day = selected_day.isoformat() if selected_day else "all"

    # Same conditional-GET treatment as /live and /bess -- this page is
    # kept current by the same background_refresh cycle (see
    # ingest_forecast_medium_term()), so a reload between cycles can skip
    # straight to a 304 before running any of the real queries below. The
    # ETag folds in `day` since that changes the rendered output on its
    # own, same reasoning as BESS's window/auction_day.
    last_updated = latest_fetch_ts(db) or ""
    etag = http_cache.etag_for(last_updated, day)
    cached = http_cache.not_modified(request, etag)
    if cached is not None:
        return cached

    charts = []
    for c in CHARTS:
        points = c["fn"](db, today, selected_day)
        if selected_day is not None:
            svg = charts_live.progression_svg(points, c["color"], c["unit"])
        else:
            svg = charts_live.multi_day_forecast_svg(points, c["color"], c["unit"])
        charts.append({
            "key": c["key"],
            "label": c["label"],
            "unit": c["unit"],
            "svg": svg,
            "has_data": bool(points),
            "stats": fm.stat_summary(points),
            "show_min": c["show_min"],
        })

    # Nuclear is naturally a "one number per day" figure, not a per-period
    # series -- reacts to the same shared `day` filter as every chart
    # above, just rendered as a table across the whole window when "all"
    # is selected, or a single figure for the one day in focus otherwise
    # (see forecasts.html). next(..., None) rather than a dict lookup --
    # a day with nothing ingested yet (or day 1, which this dataset never
    # covers at all) is a real, expected "no figure for this day" case,
    # not an error.
    nuclear_by_day = fm.nuclear_forecast_by_day(db, today)
    nuclear_selected = next((n["value"] for n in nuclear_by_day if n["date"] == selected_day), None)

    context = {
        "request": request,
        "active_nav": "forecasts",
        "today": today,
        "days": days,
        "day": day,
        "all_available": all_available,
        "charts": charts,
        "nuclear_by_day": nuclear_by_day,
        "nuclear_selected": nuclear_selected,
    }
    response = templates.TemplateResponse(request, "forecasts.html", context)
    http_cache.apply_cache_headers(response, etag)
    return response
