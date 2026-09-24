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
from ..storage import latest_fetch_ts
from . import charts_bess, http_cache
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Jinja's built-in '%.0f'|format has no thousands-separator equivalent --
# every large number on this page (MW volumes, £ revenue) needs one.
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}"


def _latest_settled_date(db: sqlite3.Connection) -> date | None:
    """MAX(sd) in eac_results, capped at today (None if no rows at all --
    preserved as-is so the page's own "No EAC data ingested yet" check
    still works). eac_results can now genuinely hold a day-ahead row for
    tomorrow (background_refresh.py's EAC ingest window runs one day past
    `today` on purpose -- see its own comment), so the raw MAX(sd) is no
    longer always "the latest settled day"; the window picker and the
    "Data to ..." stamp both mean the latter, not "including tomorrow's
    still-clearing auction".
    """
    latest = eac_metrics.latest_available_date(db)
    return min(latest, date.today()) if latest else None


def _window_dates(db: sqlite3.Connection, window: int) -> tuple[date, date]:
    end = _latest_settled_date(db) or date.today()
    start = end - timedelta(days=window - 1)
    return start, end


@router.get("/bess", response_class=HTMLResponse)
def bess_page(request: Request, window: int = 7, auction_day: str = "today", db: sqlite3.Connection = Depends(get_db)):
    # Cheap freshness check before any of the real queries below -- see
    # http_cache.py. The ETag has to cover window/auction_day too, not
    # just last_updated, since those change the rendered output on their
    # own regardless of whether any new data has landed.
    last_updated = latest_fetch_ts(db) or ""
    etag = http_cache.etag_for(last_updated, window, auction_day)
    cached = http_cache.not_modified(request, etag)
    if cached is not None:
        return cached

    start, end = _window_dates(db, window)

    summary = eac_metrics.market_summary(db, start, end)
    dist = eac_metrics.distribution(db, start, end)
    activity = bm_metrics.bm_activity(db, start, end)

    # Independent of `window` above by design -- see the plan this shipped
    # from. The window picker drives EAC summary + BM activity together;
    # "today's auctions" is EAC-only and meant to be glanced at repeatedly
    # through the day, so it must not move when someone picks a different
    # window to analyze trends with.
    #
    # EAC auctions clear day-ahead -- by the time "today" is well underway,
    # tomorrow's auction has typically already cleared and is sitting in
    # eac_results too, so a user glancing at this card mid-day can easily
    # land on tomorrow's figures without realising the card moved. Rather
    # than silently pick one, `auction_day` lets the user flip between the
    # two explicitly -- both cards are labelled with the actual calendar
    # date they show (see bess_analytics.html), not just "Today"/"Tomorrow".
    today = date.today()
    tomorrow = today + timedelta(days=1)
    auction_day = auction_day if auction_day in ("today", "tomorrow") else "today"
    selected_day = tomorrow if auction_day == "tomorrow" else today
    today_revenue = eac_metrics.daily_revenue_by_participant(db, selected_day)

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
        "latest_available": _latest_settled_date(db),
        "summary": summary,
        "dist": dist,
        "activity": activity,
        "today": today,
        "tomorrow": tomorrow,
        "auction_day": auction_day,
        "selected_day": selected_day,
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
    response = templates.TemplateResponse(request, "bess_analytics.html", context)
    http_cache.apply_cache_headers(response, etag)
    return response


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
