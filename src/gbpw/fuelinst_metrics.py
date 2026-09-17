"""
Data layer for the Live Market Generation tab -- current fuel mix and
today's progression by fuel type, both from Elexon's FUELINST dataset
(see ingest/fuelinst.py). Kept separate from live_market_metrics.py:
FUELINST's x-axis is real 5-minute UTC timestamps, not settlement-period
integers, so none of that module's per-(sd, sp) machinery applies here.

Wind is corrected twice before it's called "wind output" anywhere in this
module, both in current_mix() so every caller (the Generation Mix donut
and the Generation Type bars alike) sees the same figure:

1. Curtailment, the same way the Fundamentals tab's wind chart already
   does (see live_market_metrics.actual_plus_addon_vs_forecast()):
   FUELINST's own WIND reading is transmission-metered generation only,
   so it understates the true figure on a day with heavy curtailment.
   wind_curtailed_mw (already ingested every refresh cycle, see
   ingest/wind_curtailment.py) is added on at whichever settlement
   period each FUELINST reading falls in -- if that addon hasn't
   published yet for a given period, the raw FUELINST value is used
   as-is (not a fabricated zero-curtailment assumption, just the best
   currently-known figure), matching this project's general "unknown is
   not confirmed zero" discipline.
2. Embedded wind: FUELINST's WIND reading is transmission-connected wind
   only, same reason FUELINST has no solar category at all -- small,
   distribution-connected wind never gets BM-metered. NESO's own
   embedded wind forecast (`wind_embedded_forecast`, see
   ingest/neso_embedded.py) is added on top so "wind output" means the
   whole GB wind fleet, not just the transmission-connected part.
   Unlike solar (which has no FUELINST counterpart to combine with, so
   it's kept as its own segment downstream in current_mix_by_category()),
   wind already has a FUELINST entry to add onto.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from .ingest.elexon import INTERCONNECTORS
from .live_market_metrics import today_progression
from .settlement import utc_to_settlement
from .storage import series_for_week

WIND_FUEL_TYPE = "WIND"

# The reference dashboard's own four Generation Type categories, and the
# FUELINST fuel type each one falls into -- see the modal text quoted in
# the plan: gas/coal/oil are Fossil Fuels; wind/hydro are Renewables
# (solar is added separately below, since it isn't a FUELINST fuel type
# at all); nuclear is Low Carbon; biomass, Misc (OTHER) and Pumped
# Storage are Other.
CATEGORIES = ("Fossil Fuels", "Renewables", "Low Carbon", "Other")

FUEL_TO_CATEGORY: dict[str, str] = {
    "CCGT": "Fossil Fuels", "OCGT": "Fossil Fuels", "COAL": "Fossil Fuels", "OIL": "Fossil Fuels",
    "WIND": "Renewables", "NPSHYD": "Renewables",
    "NUCLEAR": "Low Carbon",
    "BIOMASS": "Other", "OTHER": "Other", "PS": "Other",
}

# Countries whose imports the reference treats as long-term low-carbon
# grids ("Norwegian and French" per its own modal text) -- every other
# import leg lands in Other. Only import (positive) legs count as a
# generation source here; export legs are excluded, same as they
# already are from total_generation/the Generation Mix donut.
LOW_CARBON_IMPORT_COUNTRIES = {"FR", "NO"}

# France is the only country with more than one interconnector
# (ifa/ifa2/eleclink -- see ingest/elexon.py's INTERCONNECTORS), and by
# direct request its 3 legs are shown as one combined "France imports"
# segment rather than 3 separate slivers.
GROUPED_IMPORT_COUNTRY = "FR"
GROUPED_IMPORT_KEY = "france"
GROUPED_IMPORT_LABEL = "France imports"


def _wind_curtailed_by_sp(conn: sqlite3.Connection, today: date) -> dict[int, float]:
    loaded = series_for_week(conn, "wind_curtailed_mw", [today])
    return {sp: v for (_sd, sp), v in loaded.items()}


def _parse_utc(start_time: str) -> datetime:
    dt = datetime.fromisoformat(start_time)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _wind_adjusted(fuel_type: str, mw: float, start_time: str, curtailed_by_sp: dict[int, float]) -> float:
    if fuel_type != WIND_FUEL_TYPE:
        return mw
    _sd, sp = utc_to_settlement(_parse_utc(start_time))
    return mw + curtailed_by_sp.get(sp, 0.0)


def current_mix(conn: sqlite3.Connection, today: date, include_embedded_wind: bool = True) -> dict:
    """Latest published FUELINST reading per fuel type -- {start_time,
    by_fuel: [{fuel_type, generation_mw}, ...]}, sorted largest first.
    Empty by_fuel (start_time None) if nothing has been ingested yet.
    WIND already carries the curtailment correction described in the
    module docstring, and (when `include_embedded_wind` is True, the
    default) the embedded-wind one too.

    `include_embedded_wind=False` is for callers that already show
    embedded wind as its own, separate figure -- GB Power Flow's "LV
    Wind" ring, see power_flow_metrics.py -- where adding it into WIND
    here as well would double-count it. Generation Mix and Generation
    Type have no such separate figure, so they use the default.
    """
    row = conn.execute("SELECT MAX(start_time) FROM fuelinst_generation").fetchone()
    start_time = row[0] if row else None
    if start_time is None:
        return {"start_time": None, "by_fuel": []}

    rows = conn.execute(
        "SELECT fuel_type, generation_mw FROM fuelinst_generation WHERE start_time = ?",
        (start_time,),
    ).fetchall()
    curtailed_by_sp = _wind_curtailed_by_sp(conn, today)
    embedded_wind = today_progression(conn, "wind_embedded_forecast", today)["latest_value"] or 0.0 if include_embedded_wind else 0.0
    by_fuel = []
    for fuel_type, mw in rows:
        adjusted = _wind_adjusted(fuel_type, mw, start_time, curtailed_by_sp)
        if fuel_type == WIND_FUEL_TYPE:
            adjusted += embedded_wind
        by_fuel.append({"fuel_type": fuel_type, "generation_mw": round(adjusted, 1)})
    by_fuel.sort(key=lambda r: r["generation_mw"], reverse=True)
    return {"start_time": start_time, "by_fuel": by_fuel}


def current_mix_by_category(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Today's current generation bucketed into the four Generation Type
    categories -- [{category, generation_mw, segments}, ...], always all
    four categories (even at 0), matching the reference's fixed 4-bar
    x-axis. `generation_mw` is the category's real total (every
    contributor, including a momentarily-negative one like pumped
    storage charging); `segments` is the breakdown that actually gets
    drawn as a stacked sub-bar -- [{kind, key, label, generation_mw}],
    largest first, **positive contributors only** (same "can't draw a
    negative slice" rule the donuts already apply), so a category's
    drawn segments can sum to slightly less than its own total on a
    heavy-charging period.

    Sources: current_mix()'s own FUELINST reading (wind already
    curtailment- and embedded-wind-adjusted, see the module docstring)
    via FUEL_TO_CATEGORY (kind="fuel", key=fuel type code); embedded
    solar (the `solar` series -- not a FUELINST fuel type at all, added
    here since the reference's own Generation Type includes it) as its
    own Renewables segment (kind="solar"); and today's latest *import*
    leg per interconnector (kind="import", key=interconnector key,
    label=its display name) via LOW_CARBON_IMPORT_COUNTRIES -- except
    France's 3 legs (ifa/ifa2/eleclink), which are summed into one
    "France imports" segment (GROUPED_IMPORT_COUNTRY) rather than shown
    as 3 separate slivers.
    """
    totals = {c: 0.0 for c in CATEGORIES}
    segments: dict[str, list[dict]] = {c: [] for c in CATEGORIES}

    mix = current_mix(conn, today)
    for r in mix["by_fuel"]:
        category = FUEL_TO_CATEGORY.get(r["fuel_type"], "Other")
        totals[category] += r["generation_mw"]
        if r["generation_mw"] > 0:
            segments[category].append({"kind": "fuel", "key": r["fuel_type"], "label": None, "generation_mw": r["generation_mw"]})

    solar_latest = today_progression(conn, "solar", today)["latest_value"]
    if solar_latest is not None:
        totals["Renewables"] += solar_latest
        if solar_latest > 0:
            segments["Renewables"].append({"kind": "solar", "key": "SOLAR", "label": None, "generation_mw": round(solar_latest, 1)})

    grouped_import_mw = 0.0
    grouped_category = None
    for key, name, country in INTERCONNECTORS.values():
        leg = today_progression(conn, f"interconnector_{key}_actual", today)["latest_value"]
        if leg is None or leg <= 0:
            continue
        category = "Low Carbon" if country in LOW_CARBON_IMPORT_COUNTRIES else "Other"
        totals[category] += leg
        if country == GROUPED_IMPORT_COUNTRY:
            grouped_import_mw += leg
            grouped_category = category
        else:
            segments[category].append({"kind": "import", "key": key, "label": name, "generation_mw": leg})

    if grouped_import_mw > 0:
        segments[grouped_category].append({
            "kind": "import", "key": GROUPED_IMPORT_KEY, "label": GROUPED_IMPORT_LABEL,
            "generation_mw": round(grouped_import_mw, 1),
        })

    for cat_segments in segments.values():
        cat_segments.sort(key=lambda s: s["generation_mw"], reverse=True)

    return [{"category": c, "generation_mw": round(totals[c], 1), "segments": segments[c]} for c in CATEGORIES]
