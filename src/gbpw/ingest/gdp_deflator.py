"""
HM Treasury's "GDP deflators at market prices, and money GDP" -- the
calendar-year deflator index (built from ONS outturn + OBR forecast),
used here purely as a rebasing tool: desnz_metrics.py uses it to convert
LCCC's nominal-at-the-time IMRP outturn and DESNZ's constant-2024-price
Annex M scenarios onto one common "today's money" basis for the PPA
Tools long-term price chart. This is the same series DESNZ itself uses
to state Annex M "in 2024 prices" (see ingest/desnz_eep.py).

One current release, hand-updated when Treasury publishes the next one
(same disclosed-registry precedent as desnz_eep.py's VINTAGES) -- a
quarterly-ish release, not a live feed, so this is never wired into
background_refresh.py's 5-minute cycle either.

The calendar-year block gives a real index LEVEL only for outturn years
(through 2025 as of the June 2026 release); years beyond that are given
only as a %-change on the prior year -- OBR's own forecast horizon.
_build_index() compounds those forward from the last real level using
Treasury's own published %-change figures -- an explicit, disclosed
one-step-per-year extrapolation, not an invented rate. A year with
neither a level nor a %-change (beyond the forecast horizon entirely)
is simply absent from the index, not guessed.
"""

from __future__ import annotations

import io
import re

import openpyxl
import requests

from ..storage import GdpDeflatorRow

LATEST = {
    "vintage": "2026-06",
    "url": "https://assets.publishing.service.gov.uk/media/6a43dbc7167a99cf0018d9a0/GDP_Deflators_Qtrly_National_Accounts_June_2026_update.xlsx",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gbpw-ingest)"}
TIMEOUT = 60

# Calendar-year block's column layout (0-indexed), confirmed live against
# the real June 2026 file -- col H is the year label, col I the index
# level (real years) or '-' (forecast years), col J the %-change.
COL_YEAR = 7
COL_LEVEL = 8
COL_PCT = 9

_YEAR_RE = re.compile(r"^(\d{4})")


def _extract_calendar_year_series(ws) -> dict[int, tuple[float | None, float | None]]:
    """Scans every row for the calendar-year block's data rows -- a year
    label can be a plain int (real outturn years) or a string like
    '2026 (1), (2)' (Treasury's own footnote markers on forecast years),
    so the year is pulled via regex rather than assuming a type. Returns
    {year: (level_or_None, pct_change_or_None)}.
    """
    out: dict[int, tuple[float | None, float | None]] = {}
    for row in ws.iter_rows(values_only=True):
        if len(row) <= COL_PCT:
            continue
        year_cell = row[COL_YEAR]
        if year_cell is None:
            continue
        match = _YEAR_RE.match(str(year_cell).strip())
        if not match:
            continue
        year = int(match.group(1))
        level = row[COL_LEVEL]
        level = float(level) if isinstance(level, (int, float)) else None
        pct = row[COL_PCT]
        pct = float(pct) if isinstance(pct, (int, float)) else None
        out[year] = (level, pct)
    return out


def _build_index(raw: dict[int, tuple[float | None, float | None]]) -> tuple[dict[int, float], set[int]]:
    """Pure -- real levels pass through unchanged; a year with only a
    %-change compounds forward from the immediately preceding year's
    already-resolved index. Years processed in ascending order so each
    forecast year's prior-year index is guaranteed resolved first (the
    source data is contiguous, so there's no gap to bridge). Returns
    (index, forecast_years) -- forecast_years is exactly the set that
    had to be compounded rather than read as a real level.
    """
    index: dict[int, float] = {}
    forecast_years: set[int] = set()
    for year in sorted(raw):
        level, pct = raw[year]
        if level is not None:
            index[year] = level
        elif pct is not None and (year - 1) in index:
            index[year] = index[year - 1] * (1 + pct / 100)
            forecast_years.add(year)
        # else: neither a level nor a usable prior-year+pct -- left out entirely.
    return index, forecast_years


def fetch_latest() -> tuple[list[GdpDeflatorRow], str]:
    resp = requests.get(LATEST["url"], headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=True)
    ws = wb.active

    raw = _extract_calendar_year_series(ws)
    index, forecast_years = _build_index(raw)

    vintage = LATEST["vintage"]
    rows = [
        GdpDeflatorRow(year=year, vintage=vintage, deflator=value, is_forecast=year in forecast_years)
        for year, value in sorted(index.items())
    ]
    note = f"ok ({len(rows)} year(s), {len(forecast_years)} forecast, vintage {vintage})"
    return rows, note
