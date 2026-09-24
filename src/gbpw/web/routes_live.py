"""
Live Market: the in-progress week (Monday through yesterday) plus today's
live, partial progression, split into three tabs -- Fundamentals (imbalance,
wind/solar/demand actual vs forecast), Interconnectors (per-link
scheduled vs actual), and Generation. The Generation tab itself has four
cards, in the same order as the energydashboard.co.uk reference it's
modeled on: Generation Mix (FUELINST fuel mix, see fuelinst_metrics.py),
Generation Type (the same mix bucketed into 4 fixed categories), Carbon
Intensity (NESO's separate Carbon Intensity API, see
carbon_intensity_metrics.py), and GB Power Flow (a rings/flow diagram
composed from series other tabs already ingest, see power_flow_metrics.py).
Phase 2 (solar, demand cross-check, wind/demand/solar forecasts,
interconnector actual) and Phase 3 (ENTSO-E/SEMO scheduled flows) are both
wired in -- see ingest/neso_embedded.py, entsoe_flows.py, semo_flows.py.

/live/status backs the page's own polling script (see live_market.html's
extra_body block) -- it reloads only when a background refresh cycle has
actually landed new data, not on a blind fixed-interval timer.

Wind has no standalone "raw actual" KPI card -- only `wind_true_cmp`
(actual + instructed-shut volume, lagging ~18 minutes behind real time,
Elexon's own bid-acceptance publish delay -- see live_market_metrics.
actual_plus_addon_vs_forecast()) gets one, so the KPI row stays at 5
cards. The chart itself (`wind_triple_svg`) still shows both the raw
actual line and the +curtailed line together on one set of axes
(actual_and_addon_vs_forecast()) -- the two solid lines will genuinely
end at different settlement periods given the addon's publish lag;
that's disclosed on the chart, not hidden by giving each its own card.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import carbon_intensity_metrics as cim
from .. import fuelinst_metrics as fim
from .. import live_market_metrics as lmm
from .. import power_flow_metrics as pfm
from ..ingest.elexon import INTERCONNECTORS
from ..storage import latest_fetch_ts
from . import charts_generation, charts_live, http_cache
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"
# None-safe MW->GW conversion for GB Power Flow's ring values, which can
# genuinely be None (a figure not published yet today) -- chains with
# |commas so a missing ring reads "—" rather than raising on None/1000.
templates.env.filters["gw"] = lambda v: None if v is None else v / 1000

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
    # Cheap freshness check before any of the real queries below -- a
    # reload that lands between two background-refresh cycles (the
    # common case for a tab left open) short-circuits into a 304 here
    # instead of re-running everything just to rebuild identical HTML.
    last_updated = latest_fetch_ts(db) or ""
    etag = http_cache.etag_for(last_updated)
    cached = http_cache.not_modified(request, etag)
    if cached is not None:
        return cached

    today = date.today()
    week_range = lmm.week_so_far(today)
    dates = list(_date_range(*week_range)) if week_range else []

    imbalance_today = lmm.today_progression(db, "imbalance", today)
    imbalance_delta = lmm.delta_vs_yesterday(db, "imbalance", today)
    imbalance_volume_today = lmm.today_progression(db, "imbalance_volume", today)
    # Price (£/MWh) and net imbalance volume (MWh) don't share a unit --
    # a single shared y-axis would flatten whichever has the smaller
    # range, so this gets its own dual-axis chart, not a third line
    # squeezed onto an existing one.
    imbalance_dual = lmm.dual_series_today(db, "imbalance", "imbalance_volume", today)

    # wind_curtailed_mw (capacity instructed off via the Balancing
    # Mechanism, see ingest/wind_curtailment.py) lags real time by ~18
    # minutes -- Elexon's own bid-acceptance publish delay -- so it
    # updates on a genuinely different cadence than the fast-updating raw
    # wind actual. Merging them into one line would make that lag look
    # like a data dropout in the actual series; kept as its own chart line
    # instead (see wind_triple below).
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
        regions = lmm.deviation_regions(cmp["points"])
        # Only the deviation happening right now, if any -- a run that
        # ended earlier today (flow's back on schedule since) isn't shown
        # at all, rather than as a stale "was overperforming since SP3"
        # note that no longer describes what's happening. This page's
        # figures are a live, current-state picture throughout; this is
        # the same treatment, not a running log of today's deviations.
        active_deviation = regions[-1] if regions and regions[-1]["end_sp"] == cmp["latest_sp"] else None
        interconnectors.append({
            "key": key,
            "name": name,
            "country": country,
            "cmp": cmp,
            "cmp_svg": charts_live.comparison_svg(cmp["points"], "var(--lm-violet)", "var(--lm-dim)", "MW"),
            "active_deviation": active_deviation,
        })

    mix = fim.current_mix(db, today)
    # A fuel type can be momentarily negative (pumped storage while
    # charging) -- a donut can't represent a negative share of a whole,
    # so those are excluded from the chart, same split of responsibility
    # as routes_bess.py filtering non-positive revenue before the donut.
    # "Total generation" itself still sums every reading, negative
    # included, matching NESO's own fuel-mix framing (see the plan's
    # Scope decision #2) -- but the legend's own percentages are each
    # slice's share of the donut's whole (positive slices only, same
    # denominator charts_generation.generation_mix_donut_svg() uses for
    # its own arcs), not share of that net total. Using the net total
    # there instead would make the legend and the donut's own inline
    # percentage labels disagree, and the legend's percentages wouldn't
    # sum to 100%.
    mix_slices = [r for r in mix["by_fuel"] if r["generation_mw"] > 0]
    mix_positive_total_mw = sum(r["generation_mw"] for r in mix_slices) or 1.0
    generation_legend = [
        {
            "fuel_type": charts_generation.fuel_label(r["fuel_type"]),
            "generation_mw": r["generation_mw"],
            "pct": r["generation_mw"] / mix_positive_total_mw * 100,
            "color": charts_generation.fuel_color(r["fuel_type"]),
        }
        for r in sorted(mix_slices, key=lambda r: r["generation_mw"], reverse=True)[:8]
    ]

    category_mix = fim.current_mix_by_category(db, today)

    intensity = cim.current_intensity(db, today)
    ci_emissions_mix = cim.emissions_mix(db, today)
    # Rendered directly inside the donut's own hole (see live_market.html's
    # .ci-donut-label), matching the reference's own centered CI figure --
    # not a separate KPI card above the chart.
    ci_center_label = (
        f"{intensity['forecast']:,.0f} gCO2/kWh ({intensity['index_label']})" if intensity else "No data yet"
    )

    flow = pfm.current_flow(db, today)
    # Rendered inside the sources donut's own hole, same "centered
    # figure" treatment as Carbon Intensity's ci_center_label above.
    pf_center_label = (
        f"{flow['gb_production_mw'] / 1000:,.2f} GW" if flow.get("gb_production_mw") is not None else "No data yet"
    )
    # Per-country breakdown, shown only via a native title-attribute
    # tooltip on hover over the Imports/Exports rings themselves --
    # replaces the always-visible far-left/far-right country list and
    # its own per-leg connecting lines, which cluttered the diagram.
    import_tooltip = "\n".join(f"{leg['name']}: {leg['value'] / 1000:,.2f} GW" for leg in flow["import_legs"]) or "No imports yet today"
    export_tooltip = "\n".join(f"{leg['name']}: {leg['value'] / 1000:,.2f} GW" for leg in flow["export_legs"]) or "No exports yet today"

    context = {
        "request": request,
        "active_nav": "live",
        "today": today,
        "week_range": week_range,
        "fundamentals": FUNDAMENTALS,
        "combined_days": combined_days,
        "imbalance_today": imbalance_today,
        "imbalance_delta": imbalance_delta,
        "imbalance_volume_today": imbalance_volume_today,
        "imbalance_dual_svg": charts_live.dual_series_svg(
            imbalance_dual["points"], "var(--lm-amber)", "var(--lm-violet)", "£/MWh", "MWh"
        ),
        "wind_true_cmp": wind_true_cmp,
        "wind_triple_svg": charts_live.triple_comparison_svg(
            wind_triple["points"], "var(--lm-teal)", "var(--lm-violet)", "var(--lm-dim)", "MW"
        ),
        "demand_cmp": demand_cmp,
        "demand_cmp_svg": charts_live.comparison_svg(demand_cmp["points"], "var(--lm-violet)", "var(--lm-dim)", "MW"),
        "solar_cmp": solar_cmp,
        "solar_cmp_svg": charts_live.comparison_svg(solar_cmp["points"], "var(--lm-gold)", "var(--lm-dim)", "MW"),
        "interconnectors": interconnectors,
        "generation_mix_svg": charts_generation.generation_mix_donut_svg(mix_slices),
        "generation_legend": generation_legend,
        "category_mix": category_mix,
        "generation_category_svg": charts_generation.generation_category_bars_svg(category_mix),
        "intensity": intensity,
        "ci_emissions_mix": ci_emissions_mix,
        "ci_center_label": ci_center_label,
        "carbon_intensity_svg": charts_generation.carbon_intensity_donut_svg(ci_emissions_mix),
        "ci_legend": [
            {
                "label": charts_generation.segment_label(s),
                "pct": s["pct"],
                "color": charts_generation.segment_color(s),
            }
            for s in ci_emissions_mix[:8]
        ],
        "flow": flow,
        "import_tooltip": import_tooltip,
        "export_tooltip": export_tooltip,
        "bm_generation_svg": charts_generation.generation_mix_donut_svg(
            flow["bm_mix"], element_id="bm-generation-donut", stroke_width=charts_generation.PF_DONUT_STROKE_WIDTH, show_labels=False
        ),
        "pf_sources_svg": charts_generation.power_flow_sources_donut_svg(flow),
        "pf_center_label": pf_center_label,
        "last_updated": last_updated,
    }
    response = templates.TemplateResponse(request, "live_market.html", context)
    http_cache.apply_cache_headers(response, etag)
    return response


@router.get("/live/status")
def live_status(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Polled by live_market.html's own JS (see extra_body block) to
    reload the page only when a background refresh cycle has actually
    completed since it was rendered -- see storage.latest_fetch_ts()."""
    return {"last_updated": latest_fetch_ts(db)}


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
