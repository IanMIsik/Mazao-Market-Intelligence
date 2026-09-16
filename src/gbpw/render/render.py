"""
facts dict + narrative dict + Jinja2 template -> one self-contained HTML file.

Every number shown comes from the facts dict (produced by metrics.build_week)
or is computed here in Python (SVG geometry). The Jinja template does no
arithmetic -- only formatting and loops. Prose (headline, byline, driver
sentences) comes from the narrative dict, sourced by build.py from
narrative_llm, narrative_rules, or a cached prior narrative -- render_week
doesn't know or care which, and renders identically regardless.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts

TEMPLATE_DIR = Path(__file__).resolve().parent
LONDON = ZoneInfo("Europe/London")


def _format_date_display(d: date) -> str:
    # avoid %-d (glibc-only, not portable to Windows) -- strip the leading zero ourselves
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


def _tz_label(local_dt: datetime) -> str:
    return "BST" if local_dt.dst() else "GMT"


def _format_dt_display(dt_utc: datetime, with_year: bool = True) -> str:
    local = dt_utc.astimezone(LONDON)
    day_month = f"{local.day} {local.strftime('%b')}"
    date_part = f"{day_month} {local.year}" if with_year else day_month
    return f"{date_part}, {local.strftime('%H:%M')} {_tz_label(local)}"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(disabled_extensions=("j2",), default=False),
    )


def render_week(facts: dict, narrative: dict, built_at_utc: datetime) -> str:
    days = facts["days"]

    hero_svg = charts.hero_chart_svg(facts["half_hourly"]["day_ahead"], facts["half_hourly"]["imbalance"], days)
    wind_spark = charts.driver_sparkline_svg(
        [d["wind_avg_gw"] for d in days], [d["dow_letter"] for d in days], "w",
        "Daily average wind output in gigawatts",
    )
    demand_spark = charts.driver_sparkline_svg(
        [d["demand_peak_gw"] for d in days], [d["dow_letter"] for d in days], "d",
        "Daily peak national demand in gigawatts",
    )
    p100_spark = charts.driver_sparkline_svg(
        [float(d["periods_above_100"]) for d in days], [d["dow_letter"] for d in days], "d",
        "Daily count of settlement periods priced above one hundred pounds",
    )
    heatmap_svg, heatmap_scale = charts.heatmap_svg(facts["heatmap"], days)

    kpi = facts["kpi"]
    week_ending_display = _format_date_display(date.fromisoformat(facts["week_ending"]))
    issued_display = _format_dt_display(built_at_utc, with_year=False)
    extracted_display = _format_dt_display(built_at_utc, with_year=True)

    context = {
        "week_ending_display": week_ending_display,
        "issued_display": issued_display,
        "extracted_display": extracted_display,
        "headline": narrative["headline"],
        "byline": narrative["byline"],
        "driver_notes": narrative["drivers"],
        "hero_svg": hero_svg,
        "wind_spark_svg": wind_spark,
        "demand_spark_svg": demand_spark,
        "p100_spark_svg": p100_spark,
        "heatmap_svg": heatmap_svg,
        "heatmap_scale": heatmap_scale,
        "kpi": kpi,
        "drivers": facts["drivers"],
        "days": days,
        "run_basis": facts["run_basis"],
        "day_ahead_gaps": facts.get("day_ahead_gaps", []),
    }
    template = _env().get_template("template.html.j2")
    return template.render(**context)
