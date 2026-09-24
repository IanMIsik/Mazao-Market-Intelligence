"""
DESNZ's Energy and Emissions Projections, Annex M ("Growth assumptions
and prices") -- the "Price - Wholesale / Electricity (baseload)" row,
p/kWh, in DESNZ's own stated real-price basis year (2024 for both
vintages known here). Three of Annex M's six scenario sheets are used,
matching Vitreous Labs' own published long-term price chart, which this
feature was modelled on: `Reference` (the department's central case)
and `FFP_Low`/`FFP_High` (fossil-fuel-price low/high, which bound the
low-to-high band). The other three sheets (`GDP_Low`, `GDP_High`,
`Existing`) aren't drawn -- same choice, not this project's own
invention.

A static, hand-maintained vintage registry, not an API poll -- DESNZ
publishes a new Annex M edition every several months to roughly a year,
each at its own fixed gov.uk asset URL, not a live feed. A future
edition needs one line added to VINTAGES once verified against that
edition's own file (same precedent as ingest/cfd_auctions.py's
PRICE_BASE_YEAR mapping not guessing at a future CfD round's price
basis). Never wired into background_refresh.py's 5-minute cycle for the
same reason -- see ingest/__init__.py's ingest_desnz_price_scenarios().

Values are stored exactly as DESNZ publishes them (2024 prices) -- NOT
inflated to nominal or "today's money" here. That conversion (via HM
Treasury's GDP deflator) happens at read time in desnz_metrics.py, kept
separate so this module's own output always traces directly to the
source file with no arithmetic applied beyond the p/kWh -> GBP/MWh unit
conversion.

Parsing is deliberately split into an odfpy-dependent extraction layer
and a pure row-building layer (same split ingest/elexon.py uses between
fetch and parse) so the row-building logic -- which is the part that
could actually have a bug -- is unit-testable with plain literal
fixtures, without needing a real .ods file in the test suite.
"""

from __future__ import annotations

import requests
from odf.opendocument import load
from odf.table import Table, TableCell, TableRow
from odf.text import P

from ..storage import DesnzPriceScenarioRow

# Verified live against the real published files (see the "long-term
# price outlook" plan): both give the wholesale electricity baseload
# row in "2024 prices", so price_base_year is fixed here rather than
# read from the file itself (Annex M doesn't state it in a machine-
# readable cell -- it's in the file's own notes/title text).
VINTAGES: dict[str, dict] = {
    "2024-12": {
        "url": "https://assets.publishing.service.gov.uk/media/6751eae76da7a3435fecbd8e/Annex_M_assumptions_growth_price.ods",
        "price_base_year": 2024,
    },
    "2026-02": {
        "url": "https://assets.publishing.service.gov.uk/media/6981d725f2ea511998158f75/Annex_M_assumptions_growth_price.ods",
        "price_base_year": 2024,
    },
}

SHEET_SCENARIOS: dict[str, str] = {"Reference": "reference", "FFP_Low": "ffp_low", "FFP_High": "ffp_high"}

METRIC_PREFIX = "Price - Wholesale"
FUEL_MATCH = "Electricity (baseload)"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gbpw-ingest)"}
TIMEOUT = 60


def _cell_text(cell: TableCell) -> str:
    return "".join(str(p) for p in cell.getElementsByType(P))


def _cell_float(cell: TableCell) -> float | None:
    value = cell.getAttribute("value")
    if value is None:
        return None
    return float(value)


def _extract_baseload_series(sheet: Table) -> dict[int, float]:
    """Scans `sheet` for the header row (year labels) and the baseload
    electricity wholesale-price row, located by content match rather
    than a hardcoded row index -- confirmed live it's row 19 in the Feb
    2026 file, but nothing in the source guarantees that position is
    stable across vintages. Returns {year: p/kWh value}; a row/column
    that doesn't parse as expected is simply not included, not guessed.
    """
    rows = sheet.getElementsByType(TableRow)
    year_by_col: dict[int, int] = {}
    baseload_by_col: dict[int, float] = {}

    for row in rows:
        cells = row.getElementsByType(TableCell)
        col = 0
        expanded: list[TableCell] = []
        for cell in cells:
            repeat = cell.getAttribute("numbercolumnsrepeated")
            n = int(repeat) if repeat else 1
            expanded.extend([cell] * n)

        col0_text = _cell_text(expanded[0]) if expanded else ""
        col2_text = _cell_text(expanded[2]) if len(expanded) > 2 else ""

        if not year_by_col and col0_text == "metric":
            # Year headers are string-typed cells whose *text* is the
            # numeral (confirmed live: office:value-type="string", no
            # office:value attribute at all) -- unlike the data rows
            # below, where a year's actual reading is a real float cell.
            for c, cell in enumerate(expanded):
                text = _cell_text(cell).strip()
                if text.isdigit() and 1990 <= int(text) <= 2100:
                    year_by_col[c] = int(text)
            continue

        if col0_text.startswith(METRIC_PREFIX) and col2_text == FUEL_MATCH:
            for c, cell in enumerate(expanded):
                if c in year_by_col:
                    v = _cell_float(cell)
                    if v is not None:
                        baseload_by_col[c] = v
            break

    return {year_by_col[c]: v for c, v in baseload_by_col.items() if c in year_by_col}


def _rows_from_series(
    vintage: str, scenario: str, price_base_year: int, series: dict[int, float]
) -> list[DesnzPriceScenarioRow]:
    """Pure -- unit conversion p/kWh -> GBP/MWh is *10 (kWh->MWh is
    *1000, pence->pounds is /100, net *10). This is what tests target
    directly with literal {year: value} fixtures, no real .ods needed.
    """
    return [
        DesnzPriceScenarioRow(
            vintage=vintage, scenario=scenario, year=year, value_gbp_mwh=round(p_per_kwh * 10, 4),
            price_base_year=price_base_year,
        )
        for year, p_per_kwh in sorted(series.items())
    ]


def fetch_vintage(vintage: str) -> tuple[list[DesnzPriceScenarioRow], str]:
    if vintage not in VINTAGES:
        raise ValueError(f"unknown DESNZ EEP vintage {vintage!r}, known: {sorted(VINTAGES)}")
    info = VINTAGES[vintage]

    resp = requests.get(info["url"], headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    doc = load_from_bytes(resp.content)

    sheets = {s.getAttribute("name"): s for s in doc.spreadsheet.getElementsByType(Table)}
    rows: list[DesnzPriceScenarioRow] = []
    missing: list[str] = []
    for sheet_name, scenario in SHEET_SCENARIOS.items():
        sheet = sheets.get(sheet_name)
        if sheet is None:
            missing.append(sheet_name)
            continue
        series = _extract_baseload_series(sheet)
        rows.extend(_rows_from_series(vintage, scenario, info["price_base_year"], series))

    note = f"ok ({len(rows)} row(s) across {len(SHEET_SCENARIOS) - len(missing)} scenario sheet(s))"
    if missing:
        note += f" -- sheet(s) not found: {', '.join(missing)}"
    return rows, note


def load_from_bytes(content: bytes):
    """Thin wrapper around odf.opendocument.load so fetch_vintage() can
    be tested by monkeypatching this one function, same as every other
    ingest module's own `_get`/`_fetch_all` seam.
    """
    import io

    return load(io.BytesIO(content))


def fetch_all_known_vintages() -> tuple[list[DesnzPriceScenarioRow], str]:
    """Loops VINTAGES -- one vintage failing to download doesn't lose the
    others (own try/except per vintage), since each is an independent
    fixed file, not a paginated/resumable fetch.
    """
    rows: list[DesnzPriceScenarioRow] = []
    ok_vintages: list[str] = []
    failed: list[str] = []
    for vintage in VINTAGES:
        try:
            vintage_rows, _ = fetch_vintage(vintage)
            rows.extend(vintage_rows)
            ok_vintages.append(vintage)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{vintage} ({e})")

    note = f"ok ({len(rows)} row(s) across vintage(s): {', '.join(ok_vintages)})"
    if failed:
        note += f" -- failed: {'; '.join(failed)}"
    return rows, note
