"""
Data layer for the Live Market Generation tab's GB Power Flow card --
composes numbers other modules already expose (total_generation, demand,
embedded wind/solar, interconnector legs, FUELINST's PS reading) into the
ring values the reference dashboard's power-flow diagram shows. No new
low-level queries here: this module is pure composition.

Station Load (a generator's own auxiliary consumption) has no identified
public GB-wide data source, so it isn't shown as its own ring -- it's
folded into `other_demand_mw` below along with distribution-level demand
and transmission losses, disclosed as a residual rather than fabricated.

Pumped storage's *discharge* (generating) is already inside
`total_generation` -- FUELHH/FUELINST's PS reading is signed and
total_generation sums every fuel type as-is (see ingest/elexon.py's
fetch_generation()). Only the *charging* case (PS negative) is split out
here, as its own demand-side figure, same as the reference does.

`bm_mix` is BM Generation's own fuel-type breakdown (for its donut on
the card), sourced from fuelinst_metrics.current_mix() with
include_embedded_wind=False -- embedded wind is already its own
separate "LV Wind" figure here, so folding it into this donut's WIND
slice too would double-count it. Positive contributors only, same
"can't draw a negative slice" rule the other donuts on this page apply.

Total Demand uses `demand_itsdo` (ITSDO, transmission-metered demand)
plus embedded wind/solar, not `demand` (INDO) -- live-verified while
building this: INDO alone came out *lower* than ITSDO (19.4 GW vs
24.1 GW at the same instant), and GB Production + Imports (~37.2 GW)
only reconciles against Total Demand + Exports (~37.4 GW using ITSDO +
embedded, vs ~22.7 GW using INDO alone) within a normal few-hundred-MW
grid-loss margin when ITSDO is the base -- consistent with ITSDO being
what's metered *at* the transmission system, with embedded generation's
own locally-served demand (never touching transmission) added back to
get true total consumption, matching what GB Production already adds on
the supply side.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import fuelinst_metrics as fim
from .ingest.elexon import INTERCONNECTORS
from .live_market_metrics import today_progression

PS_FUEL_TYPE = "PS"


def _interconnector_legs(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Today's latest reading per interconnector link -- [{key, name,
    country, value}], value signed (+import/-export). A link with
    nothing published today is simply absent, not shown as a fabricated
    zero-flow leg -- same honesty convention as the Interconnectors tab.
    """
    legs = []
    for key, name, country in sorted(INTERCONNECTORS.values(), key=lambda v: v[1]):
        latest = today_progression(conn, f"interconnector_{key}_actual", today)["latest_value"]
        if latest is not None:
            legs.append({"key": key, "name": name, "country": country, "value": latest})
    return legs


def current_flow(conn: sqlite3.Connection, today: date) -> dict:
    """Every ring/summary value GB Power Flow needs, for `today`'s latest
    reading of each underlying series. The two headline figures
    (`gb_production_mw`, `net_demand_mw`/`other_demand_mw`) are None
    when their required input (`total_generation`/`demand_itsdo`) hasn't
    published yet today -- not silently treated as zero -- while the
    smaller embedded wind/solar additions fall back to 0 contribution if
    missing, same "best currently-known figure" convention
    fuelinst_metrics.py already uses for wind curtailment.
    """
    total_generation = today_progression(conn, "total_generation", today)["latest_value"]
    embedded_wind = today_progression(conn, "wind_embedded_forecast", today)["latest_value"]
    embedded_solar = today_progression(conn, "solar", today)["latest_value"]
    transmission_demand = today_progression(conn, "demand_itsdo", today)["latest_value"]
    total_demand = (
        None if transmission_demand is None
        else transmission_demand + (embedded_wind or 0.0) + (embedded_solar or 0.0)
    )

    legs = _interconnector_legs(conn, today)
    import_legs = [leg for leg in legs if leg["value"] > 0]
    export_legs = [{**leg, "value": abs(leg["value"])} for leg in legs if leg["value"] < 0]
    imports_mw = sum(leg["value"] for leg in import_legs)
    exports_mw = sum(leg["value"] for leg in export_legs)

    # BM Generation's own fuel-type breakdown, for its donut on the
    # card -- include_embedded_wind=False since embedded wind is
    # already its own separate "LV Wind" node on this diagram; merging
    # it into this donut's WIND slice too would double-count it.
    bm_by_fuel = fim.current_mix(conn, today, include_embedded_wind=False)["by_fuel"]
    bm_mix = [r for r in bm_by_fuel if r["generation_mw"] > 0]

    ps_latest = next((r["generation_mw"] for r in bm_by_fuel if r["fuel_type"] == PS_FUEL_TYPE), None)
    pumped_storage_charge_mw = abs(ps_latest) if ps_latest is not None and ps_latest < 0 else 0.0

    gb_production_mw = (
        None if total_generation is None
        else total_generation + (embedded_wind or 0.0) + (embedded_solar or 0.0)
    )
    net_demand_mw = None if total_demand is None else total_demand - exports_mw
    other_demand_mw = (
        None if total_demand is None
        else max(total_demand - exports_mw - pumped_storage_charge_mw, 0.0)
    )

    return {
        "bm_generation_mw": total_generation,
        "bm_mix": bm_mix,
        "embedded_wind_mw": embedded_wind,
        "embedded_solar_mw": embedded_solar,
        "imports_mw": round(imports_mw, 1),
        "exports_mw": round(exports_mw, 1),
        "import_legs": import_legs,
        "export_legs": export_legs,
        "pumped_storage_charge_mw": round(pumped_storage_charge_mw, 1),
        "total_demand_mw": None if total_demand is None else round(total_demand, 1),
        "net_demand_mw": None if net_demand_mw is None else round(net_demand_mw, 1),
        "gb_production_mw": None if gb_production_mw is None else round(gb_production_mw, 1),
        "other_demand_mw": None if other_demand_mw is None else round(other_demand_mw, 1),
    }
