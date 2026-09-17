"""
Data layer for the Live Market Generation tab's Carbon Intensity card --
the headline gCO2/kWh figure (current_intensity(), own small table, see
storage.py) and the donut's emissions-weighted breakdown by source
(emissions_mix()).

emissions_mix() deliberately does NOT use the Carbon Intensity API's own
/generation endpoint -- per the reference dashboard's own methodology
("The donut slices show how different sources contribute to those
emissions by %. They use the latest 5-minute generation data"), the
breakdown is built from FUELINST's own 5-minute mix
(fuelinst_metrics.current_mix(), the same data the Generation Mix donut
uses) plus embedded solar and interconnector import legs, each weighted
by its official NESO CI factor (ingest/carbon_intensity.py's
/intensity/factors, stored in `carbon_intensity_factors`) to get a share
of *emissions*, not a share of generation -- a 5% share of generation
from coal contributes far more than 5% of emissions.

Imports are split by country where NESO publishes a factor for one
(France, Netherlands, Ireland only, confirmed live) -- CI varies a lot
by source grid (France is mostly nuclear, low CI; Ireland has a higher
fossil share). Every other import country (Norway, Denmark, Belgium,
N. Ireland) has no published factor, so those legs are grouped into one
"Other imports" segment using the generic "Other" factor as a disclosed
approximation, rather than guessing a country-specific number NESO
itself doesn't publish. Battery/Pumped Storage are zero CI at point of
generation per the reference's own text -- already true here since
NESO's own published Pumped Storage factor is 0 (this project doesn't
split a separate Battery reading out of FUELINST's OTHER category, so
there's nothing further to zero out for battery specifically).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import fuelinst_metrics as fim
from .ingest.elexon import INTERCONNECTORS
from .live_market_metrics import today_progression

# Countries NESO publishes an import CI factor for (see
# ingest/carbon_intensity.py's FACTOR_KEY_MAP) -- country code -> our
# own factor key and a display label for the grouped segment.
IMPORT_FACTOR_COUNTRIES: dict[str, tuple[str, str]] = {
    "FR": ("IMPORT_FR", "France imports"),
    "NL": ("IMPORT_NL", "Netherlands imports"),
    "IRL": ("IMPORT_IRL", "Ireland imports"),
}
OTHER_IMPORTS_LABEL = "Other imports"
OTHER_IMPORTS_FACTOR_KEY = "OTHER"  # no published country factor for Norwegian/Danish/Belgian/N.Irish links -- the generic "Other" factor is a disclosed approximation, not a guessed one


def current_intensity(conn: sqlite3.Connection, today: date) -> dict | None:
    """The latest carbon_intensity row for `today` -- {sd, sp, forecast,
    actual, index_label}, or None if nothing has been ingested yet.
    """
    row = conn.execute(
        "SELECT sd, sp, forecast, actual, index_label FROM carbon_intensity WHERE sd = ? ORDER BY sp DESC LIMIT 1",
        (today.isoformat(),),
    ).fetchone()
    if row is None:
        return None
    sd, sp, forecast, actual, index_label = row
    return {"sd": sd, "sp": sp, "forecast": forecast, "actual": actual, "index_label": index_label}


def _factors(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT key, factor FROM carbon_intensity_factors").fetchall()
    return dict(rows)


def emissions_mix(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Today's latest generation, one entry per emitting source --
    [{kind, key, label, generation_mw, factor, emissions, pct}, ...],
    sorted by emissions share descending. `pct` is share of *emissions*
    (generation_mw * factor), not share of generation. A source with no
    published factor yet (factors table empty, or a genuinely new
    FUELINST fuel type NESO hasn't published a factor for) is left out
    entirely rather than guessed at -- an empty list is a real, honest
    empty state, same as everywhere else on this page. A source with a
    published factor of exactly 0 (wind/solar/nuclear/hydro/pumped
    storage) is also left out -- it genuinely contributes nothing to
    emissions, so it isn't a permanent "0%" entry cluttering the legend.
    """
    factors = _factors(conn)
    if not factors:
        return []

    segments: list[dict] = []

    mix = fim.current_mix(conn, today)
    for r in mix["by_fuel"]:
        if r["generation_mw"] <= 0:
            continue
        factor = factors.get(r["fuel_type"])
        if factor is None:
            continue
        segments.append({
            "kind": "fuel", "key": r["fuel_type"], "label": None,
            "generation_mw": r["generation_mw"], "factor": factor,
        })

    solar_latest = today_progression(conn, "solar", today)["latest_value"]
    if solar_latest is not None and solar_latest > 0 and "SOLAR" in factors:
        segments.append({
            "kind": "solar", "key": "SOLAR", "label": None,
            "generation_mw": solar_latest, "factor": factors["SOLAR"],
        })

    import_totals: dict[str, float] = {}
    other_imports_mw = 0.0
    for key, _name, country in INTERCONNECTORS.values():
        leg = today_progression(conn, f"interconnector_{key}_actual", today)["latest_value"]
        if leg is None or leg <= 0:
            continue
        if country in IMPORT_FACTOR_COUNTRIES:
            import_totals[country] = import_totals.get(country, 0.0) + leg
        else:
            other_imports_mw += leg

    for country, mw in import_totals.items():
        factor_key, label = IMPORT_FACTOR_COUNTRIES[country]
        factor = factors.get(factor_key)
        if factor is not None:
            segments.append({"kind": "import", "key": country, "label": label, "generation_mw": mw, "factor": factor})

    if other_imports_mw > 0 and OTHER_IMPORTS_FACTOR_KEY in factors:
        segments.append({
            "kind": "other_import", "key": "OTHER_IMPORTS", "label": OTHER_IMPORTS_LABEL,
            "generation_mw": other_imports_mw, "factor": factors[OTHER_IMPORTS_FACTOR_KEY],
        })

    for seg in segments:
        seg["emissions"] = round(seg["generation_mw"] * seg["factor"], 1)

    # Zero-CI sources (wind/solar/nuclear/hydro/pumped storage, per NESO's
    # own published factors) genuinely contribute nothing to emissions --
    # dropped here rather than kept as permanent "0%" entries, which would
    # otherwise clutter the legend with several zero rows every time
    # (these are commonly a big share of GB generation).
    segments = [s for s in segments if s["emissions"] > 0]

    total_emissions = sum(s["emissions"] for s in segments) or 1.0
    for seg in segments:
        seg["pct"] = round(seg["emissions"] / total_emissions * 100, 1)

    segments.sort(key=lambda s: s["emissions"], reverse=True)
    return segments
