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
# Jinja's built-in '%.0f'|format has no thousands-separator equivalent --
# every large number on this page (MW volumes, £ revenue) needs one.
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}"


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

    # Independent of `window` above by design -- see the plan this shipped
    # from. The window picker drives EAC summary + BM activity together;
    # "today's auctions" is EAC-only and meant to be glanced at repeatedly
    # through the day, so it must not move when someone picks a different
    # window to analyze trends with.
    today = date.today()
    today_revenue = eac_metrics.daily_revenue_by_participant(db, today)

    # A donut can't represent a negative share of a whole -- clearing_price
    # can be genuinely negative (a unit paying to provide response), so
    # split those out rather than clamp or hide them from today_revenue
    # itself. Both lists stay ordered by revenue_gbp (already sorted by
    # the metrics query), so slice[:8] below is really the top 8.
    positive_participants = [r for r in today_revenue["by_participant"] if r["revenue_gbp"] > 0]
    non_positive_participants = [r for r in today_revenue["by_participant"] if r["revenue_gbp"] <= 0]
    donut_colors = charts_bess.donut_colors(len(positive_participants))
    positive_total_gbp = sum(r["revenue_gbp"] for r in positive_participants) or 1.0
    today_revenue_legend = [
        {"participant": r["participant"], "color": c, "pct": r["revenue_gbp"] / positive_total_gbp * 100}
        for r, c in zip(positive_participants[:8], donut_colors[:8])
    ]

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
        "today": today,
        "today_revenue": today_revenue,
        "today_revenue_svg": charts_bess.daily_revenue_donut_svg(positive_participants),
        "today_revenue_legend": today_revenue_legend,
        "non_positive_participants": non_positive_participants,
        "non_positive_total_gbp": sum(r["revenue_gbp"] for r in non_positive_participants),
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
