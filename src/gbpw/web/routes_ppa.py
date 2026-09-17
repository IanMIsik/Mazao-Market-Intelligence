"""
PPA Tools: wind/solar capture price & rate against LCCC's IMRP day-ahead
reference (see ppa_metrics.py, ingest/lccc.py), plus wind curtailment
risk (already ingested for the Live Market Generation tab). Backward-
looking and monthly-granularity by nature -- unlike BESS Analytics'
"today's auctions" card, there's no live/partial figure here, so this
page needs no dedicated background-refresh wiring of its own; it just
reads whatever background_refresh.py's regular cycle (which now also
covers `imrp`, see ingest/__init__.py's ingest_imrp()) has already kept
current.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import ppa_metrics
from . import charts_ppa
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"

# Longer, calendar-shaped buckets suit a capture-rate view better than
# BESS Analytics' 7/30/90-day window -- capture price is normally
# discussed monthly/annually, not as a short trailing window. A safely-
# early anchor date stands in for "all-time" rather than querying for the
# real earliest date first; the range query below simply returns nothing
# for dates before whatever's actually been ingested.
EARLIEST_POSSIBLE = date(2000, 1, 1)
WINDOWS = {"ytd": "Year to date", "12m": "Last 12 months", "3y": "Last 3 years", "all": "All-time"}
DEFAULT_WINDOW = "12m"


def _window_dates(window: str, today: date) -> tuple[date, date]:
    if window == "ytd":
        return date(today.year, 1, 1), today
    if window == "3y":
        return today - timedelta(days=3 * 365), today
    if window == "all":
        return EARLIEST_POSSIBLE, today
    return today - timedelta(days=365), today  # "12m" and any unrecognized value


@router.get("/ppa", response_class=HTMLResponse)
def ppa_page(request: Request, window: str = DEFAULT_WINDOW, db: sqlite3.Connection = Depends(get_db)):
    window = window if window in WINDOWS else DEFAULT_WINDOW
    today = date.today()
    start, end = _window_dates(window, today)

    wind = ppa_metrics.capture_price(db, "wind", start, end)
    solar = ppa_metrics.capture_price(db, "solar", start, end)
    curtailment = ppa_metrics.curtailment_risk(db, start, end)

    wind_by_month = ppa_metrics.capture_price_by_month(db, "wind", start, end)
    solar_by_month = ppa_metrics.capture_price_by_month(db, "solar", start, end)

    context = {
        "request": request,
        "active_nav": "ppa",
        "window": window,
        "windows": WINDOWS,
        "start": start,
        "end": end,
        "wind": wind,
        "solar": solar,
        "curtailment": curtailment,
        "capture_rate_trend_svg": charts_ppa.capture_rate_trend_svg(wind_by_month, solar_by_month),
    }
    return templates.TemplateResponse(request, "ppa_tools.html", context)
