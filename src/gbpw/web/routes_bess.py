"""
BESS Analytics: EAC market summary (always-on, bounded regardless of
participant count) + BM leaderboard (cashflow-only, see bm_metrics.py) +
a search-driven "Selected participants" detail, backed by two small JSON
endpoints since pre-rendering detail for thousands of participants isn't
feasible server-side.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import bm_metrics, eac_metrics
from . import charts_bess
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _window_dates(db: sqlite3.Connection, window: int) -> tuple[date, date]:
    end = eac_metrics.latest_available_date(db) or date.today()
    start = end - timedelta(days=window - 1)
    return start, end


@router.get("/bess", response_class=HTMLResponse)
def bess_page(request: Request, window: int = 7, db: sqlite3.Connection = Depends(get_db)):
    start, end = _window_dates(db, window)

    summary = eac_metrics.market_summary(db, start, end)
    dist = eac_metrics.distribution(db, start, end)
    activity = bm_metrics.bm_activity(db, start, end)

    context = {
        "request": request,
        "active_nav": "bess",
        "window": window,
        "start": start,
        "end": end,
        "latest_available": eac_metrics.latest_available_date(db),
        "summary": summary,
        "dist": dist,
        "activity": activity,
        "service_types": [r["service_type"] for r in summary["by_service_type"]],
        "market_summary_svg": charts_bess.market_summary_bars_svg(summary["by_service_type"]),
        "distribution_svg": charts_bess.distribution_histogram_svg(dist),
        "leaderboard_svg": charts_bess.leaderboard_bars_svg(activity["leaderboard"]),
        "service_type_colors": charts_bess.service_type_colors([r["service_type"] for r in summary["by_service_type"]]),
    }
    return templates.TemplateResponse(request, "bess_analytics.html", context)


@router.get("/api/eac/participants/search")
def search_participants(q: str = "", db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return [{"participant": p} for p in eac_metrics.search_participants(db, q)]


@router.get("/api/eac/participants/detail")
def participant_detail(
    p: list[str] = Query(default=[]), window: int = 7, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    start, end = _window_dates(db, window)
    eac_detail = eac_metrics.participant_detail(db, p, start, end)

    all_units: list[str] = []
    for entry in eac_detail.values():
        all_units.extend(entry["auction_units"])
    bm_units = bm_metrics.unit_detail(db, sorted(set(all_units)), start, end)

    out = {}
    for participant, entry in eac_detail.items():
        out[participant] = {
            "eac": entry,
            "bm_units": {u: bm_units[u] for u in entry["auction_units"]},
        }
    return out
