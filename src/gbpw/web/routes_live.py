"""
Live Market: the in-progress week (Monday through yesterday) plus today's
live, partial progression -- day-ahead price, imbalance, wind, demand.
Phase 1 only (see the plan) -- solar/interconnectors/forecasts land in
later phases, reusing the same day_stats()/today_progression() shape.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import live_market_metrics as lmm
from . import charts_live
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"

# Each panel: series name, display label, chart line color (matches this
# app's existing palette -- navy for price-like series, wind/demand tokens
# from app.css for the other two), and the unit shown in KPI cards.
PANELS = [
    {"series": "day_ahead", "label": "Day-ahead price", "color": "var(--navy)", "unit": "£/MWh"},
    {"series": "imbalance", "label": "Imbalance price", "color": "var(--navy-mid)", "unit": "£/MWh"},
    {"series": "wind", "label": "Wind output", "color": "var(--wind)", "unit": "MW"},
    {"series": "demand", "label": "Demand", "color": "var(--demand)", "unit": "MW"},
]


@router.get("/live", response_class=HTMLResponse)
def live_market_page(request: Request, db: sqlite3.Connection = Depends(get_db)):
    today = date.today()
    week_range = lmm.week_so_far(today)

    dates = list(_date_range(*week_range)) if week_range else []
    panels = []
    for p in PANELS:
        today_data = lmm.today_progression(db, p["series"], today)
        panels.append({
            **p,
            "today": today_data,
            "today_svg": charts_live.progression_svg(today_data["points"], p["color"], p["unit"]),
            "days": lmm.day_stats(db, p["series"], dates),
        })

    context = {
        "request": request,
        "active_nav": "live",
        "today": today,
        "week_range": week_range,
        "panels": panels,
        # One row per date, one column per panel -- built here (pure
        # reshaping of what day_stats() already computed above, not a new
        # calculation) rather than four separate near-empty tables, which
        # got harder to scan than the four independent charts above them
        # were worth. Each panel's `days` list is already the same length
        # and same date order (built from the same `dates`), so a
        # straight zip is safe.
        "combined_days": [
            {"date": dates[i].isoformat(), "by_panel": [p["days"][i] for p in panels]}
            for i in range(len(dates))
        ],
    }
    return templates.TemplateResponse(request, "live_market.html", context)


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
