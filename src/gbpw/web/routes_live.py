"""
Live Market: the in-progress week (Monday through yesterday) plus today's
live, partial progression, split into two tabs -- Fundamentals (imbalance,
wind/solar/demand actual vs forecast) and Interconnectors (per-link
scheduled vs actual). Phase 2 (solar, demand cross-check, wind/demand/solar
forecasts, interconnector actual) and Phase 3 (ENTSO-E/SEMO scheduled
flows) are both wired in -- see ingest/neso_embedded.py, entsoe_flows.py,
semo_flows.py.

Wind gets three KPI/reference figures but one chart: `wind_cmp` (raw
FUELHH actual vs forecast, for the fast-updating "Wind output" KPI) and
`wind_true_cmp` (+ instructed-shut volume, for the "Wind + curtailed" KPI,
lagging ~18 minutes behind real time -- see live_market_metrics.
actual_plus_addon_vs_forecast()) are kept separate for those KPI values,
but the chart itself (`wind_triple_svg`) draws actual, actual+curtailed,
and forecast together on one set of axes (actual_and_addon_vs_forecast()) --
the two solid lines will genuinely end at different settlement periods
given the addon's publish lag; that's disclosed on the chart, not hidden
by giving each its own card.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import live_market_metrics as lmm
from ..ingest.elexon import INTERCONNECTORS
from . import charts_live
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"

# Fundamentals daily table columns -- day-ahead intentionally dropped
# (user direction: it doesn't belong on this page). Colors are the new
# dark-theme tokens defined in app.css under body.live, not the light-theme
# navy/wind/demand vars the rest of the app uses.
FUNDAMENTALS = [
    {"series": "imbalance", "label": "Imbalance price", "color": "var(--lm-amber)", "unit": "£/MWh"},
    {"series": "wind", "label": "Wind output", "color": "var(--lm-teal)", "unit": "MW"},
    {"series": "solar", "label": "Solar output", "color": "var(--lm-gold)", "unit": "MW"},
    {"series": "demand", "label": "Demand", "color": "var(--lm-violet)", "unit": "MW"},
]


@router.get("/live", response_class=HTMLResponse)
def live_market_page(request: Request, db: sqlite3.Connection = Depends(get_db)):
    today = date.today()
    week_range = lmm.week_so_far(today)
    dates = list(_date_range(*week_range)) if week_range else []

    imbalance_today = lmm.today_progression(db, "imbalance", today)
    imbalance_delta = lmm.delta_vs_yesterday(db, "imbalance", today)

    wind_cmp = lmm.actual_vs_forecast(db, "wind", "wind_forecast", today)
    # A separate output, not merged into wind_cmp above: wind_curtailed_mw
    # (capacity instructed off via the Balancing Mechanism, see
    # ingest/wind_curtailment.py) lags real time by ~18 minutes -- Elexon's
    # own bid-acceptance publish delay -- so it updates on a genuinely
    # different cadence than the fast-updating raw wind actual. Merging
    # them into one line would make that lag look like a data dropout in
    # the actual series; kept as its own chart instead.
    wind_true_cmp = lmm.actual_plus_addon_vs_forecast(db, "wind", "wind_curtailed_mw", "wind_forecast", today)
    wind_triple = lmm.actual_and_addon_vs_forecast(db, "wind", "wind_curtailed_mw", "wind_forecast", today)
    demand_cmp = lmm.actual_vs_forecast(db, "demand", "demand_forecast", today)
    solar_cmp = lmm.actual_vs_forecast(db, "solar", "solar_forecast", today)

    fundamentals_days = {f["series"]: lmm.day_stats(db, f["series"], dates) for f in FUNDAMENTALS}
    combined_days = [
        {"date": dates[i].isoformat(), "by_series": [fundamentals_days[f["series"]][i] for f in FUNDAMENTALS]}
        for i in range(len(dates))
    ]

    interconnectors = []
    for key, name, country in sorted(INTERCONNECTORS.values(), key=lambda v: v[1]):
        cmp = lmm.actual_vs_forecast(db, f"interconnector_{key}_actual", f"interconnector_{key}_scheduled", today)
        interconnectors.append({
            "key": key,
            "name": name,
            "country": country,
            "cmp": cmp,
            "cmp_svg": charts_live.comparison_svg(cmp["points"], "var(--lm-violet)", "var(--lm-dim)", "MW"),
        })

    context = {
        "request": request,
        "active_nav": "live",
        "today": today,
        "week_range": week_range,
        "fundamentals": FUNDAMENTALS,
        "combined_days": combined_days,
        "imbalance_today": imbalance_today,
        "imbalance_delta": imbalance_delta,
        "wind_cmp": wind_cmp,
        "wind_true_cmp": wind_true_cmp,
        "wind_triple_svg": charts_live.triple_comparison_svg(
            wind_triple["points"], "var(--lm-teal)", "var(--lm-violet)", "var(--lm-dim)", "MW"
        ),
        "demand_cmp": demand_cmp,
        "demand_cmp_svg": charts_live.comparison_svg(demand_cmp["points"], "var(--lm-violet)", "var(--lm-dim)", "MW"),
        "solar_cmp": solar_cmp,
        "solar_cmp_svg": charts_live.comparison_svg(solar_cmp["points"], "var(--lm-gold)", "var(--lm-dim)", "MW"),
        "interconnectors": interconnectors,
    }
    return templates.TemplateResponse(request, "live_market.html", context)


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
