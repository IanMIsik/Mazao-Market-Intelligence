"""
SVG geometry for the PPA Tools page's capture-rate trend chart. Own
module, not folded into charts_bess.py/charts_live.py/charts_generation.py:
this chart's x-axis is calendar months spanning a multi-year window, not
settlement-period integers (charts_live.py) or a fixed 4-category axis
(charts_generation.py) or a ranked/horizontal bar (charts_bess.py).

A line chart, not bars: a full-history window can span 100+ months (once
the multi-year backfill this page depends on finishes), and capture rate
itself is a percentage that hovers in a narrow band (typically 75-110%)
rather than growing from zero -- both point away from bars (which need a
minimum legible width per category, and waste vertical space forcing a
0-based axis) and towards the same axis-building approach charts_live.py
already uses for its own settlement-period line charts: a scaled,
non-zero-based y-axis, `_y_ticks`/`_nice_step` "nice number" gridlines,
`.ax`/`.grid`/`.chart-hit` CSS classes, and a hoverable <title> per point.
The viewBox width is fixed regardless of month count (unlike this file's
previous grouped-bar version), so it scales normally via the site's
global svg{width:100%} rule -- no horizontal scrolling needed. X-axis
labels are shown sparsely (aiming for ~9 regardless of how many months
there are) since drawing one per month would overlap once there are more
than a couple dozen.

WIND_COLOR reuses app.css's existing --wind variable (already the wind
color on BESS Analytics' charts); --lm-gold (Live Market's own solar
color) is scoped to body.live only, so this page's own --solar root-level
variable was added instead, rather than reusing a color that wouldn't
resolve on a non-Live-Market page.
"""

from __future__ import annotations

import math
from datetime import date

WIND_COLOR = "var(--wind)"
SOLAR_COLOR = "var(--solar)"

AXIS_LEFT = 44.0
PLOT_W = 560.0
PLOT_TOP = 16.0
PLOT_H = 160.0
X_LABEL_Y = PLOT_TOP + PLOT_H + 14
VIEW_W = AXIS_LEFT + PLOT_W + 12
VIEW_H = X_LABEL_Y + 10
HIT_RADIUS = 3.5
TARGET_X_LABELS = 9  # aim for roughly this many visible month labels, regardless of how many months there are


def _nice_step(span: float, target_ticks: int) -> float:
    if span <= 0:
        span = 1.0
    raw = span / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = magnitude * mult
        if step >= raw:
            return step
    return magnitude * 10


def _y_ticks(min_v: float, max_v: float, target_ticks: int = 4) -> list[float]:
    """Same 'nice number' gridline rounding as charts_live.py's own
    _y_ticks() -- deliberately NOT 0-based: capture rate hovers in a
    narrow band (typically 75-110%), so forcing the axis down to 0 would
    flatten the very trend this chart exists to show.
    """
    if min_v == max_v:
        min_v, max_v = min_v - 5, max_v + 5
    step = _nice_step(max_v - min_v, target_ticks)
    lo = math.floor(min_v / step) * step
    hi = math.ceil(max_v / step) * step
    ticks = []
    t = lo
    while t <= hi + step / 2 and len(ticks) < 12:
        ticks.append(round(t, 6))
        t += step
    return ticks


def _month_label(month: str) -> str:
    """'2026-09' -> 'Sep 2026'."""
    y, m = month.split("-")
    return date(int(y), int(m), 1).strftime("%b %Y")


def _segments(months: list[str], by_month: dict[str, float], x, y) -> tuple[list[str], list[str]]:
    """One <polyline> per contiguous run of months this technology has a
    real value for -- a gap (not yet backfilled, or zero generation all
    month) breaks the line rather than drawing a fake interpolated
    segment across it. Returns (polylines, hit-circles-with-tooltips).
    """
    polylines: list[str] = []
    hits: list[str] = []
    segment: list[tuple[float, float]] = []

    def flush() -> None:
        if len(segment) > 1:
            poly = " ".join(f"{px:.1f},{py:.1f}" for px, py in segment)
            polylines.append(poly)
        segment.clear()

    for i, month in enumerate(months):
        v = by_month.get(month)
        if v is None:
            flush()
            continue
        px, py = x(i), y(v)
        segment.append((px, py))
        hits.append((px, py, month, v))
    flush()
    return polylines, hits


def _month_range(sparse_months: list[str]) -> list[str]:
    """Every calendar month from the earliest to the latest month present
    in the data, contiguous -- not just the sparse set of months that
    happen to have a value for either technology. Using this (rather than
    the sparse set directly) as the x-axis domain keeps spacing genuinely
    proportional to elapsed time: live-caught mid-backfill, wind/solar
    data currently has a real ~40-month gap (2022-08 to 2026-01 not
    ingested yet) -- indexing directly into the sparse set would silently
    compress that whole gap into one normal-width step, squashing two
    x-axis labels together and making the chart look like time passes at
    a uniform rate between plotted points when it doesn't. _segments()
    still only draws a line/point where a month has real data, so a gap
    (whether from an incomplete backfill or a genuine future data outage)
    shows as a correctly-proportioned blank stretch, not a compressed one.
    """
    first_y, first_m = (int(part) for part in sparse_months[0].split("-"))
    last_y, last_m = (int(part) for part in sparse_months[-1].split("-"))
    out = []
    y, m = first_y, first_m
    while (y, m) <= (last_y, last_m):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def capture_rate_trend_svg(wind_by_month: list[dict], solar_by_month: list[dict]) -> str:
    """Two lines (Wind/Solar), capture rate (%) by calendar month -- how
    much of the average IMRP price each technology actually captured
    that month. A month with no data for a technology simply isn't
    plotted for it, rather than a fabricated 0%.
    """
    sparse_months = sorted({r["month"] for r in wind_by_month} | {r["month"] for r in solar_by_month})
    if not sparse_months:
        return ""
    months = _month_range(sparse_months)
    wind_by = {r["month"]: r["capture_rate_pct"] for r in wind_by_month if r["capture_rate_pct"] is not None}
    solar_by = {r["month"]: r["capture_rate_pct"] for r in solar_by_month if r["capture_rate_pct"] is not None}
    values = list(wind_by.values()) + list(solar_by.values())
    if not months or not values:
        return ""

    y_ticks = _y_ticks(min(values), max(values))
    axis_min, axis_max = y_ticks[0], y_ticks[-1]
    v_span = (axis_max - axis_min) or 1.0
    n = len(months)

    def x(i: int) -> float:
        return AXIS_LEFT + ((i / (n - 1)) if n > 1 else 0.0) * PLOT_W

    def y(v: float) -> float:
        return PLOT_TOP + PLOT_H - ((v - axis_min) / v_span) * PLOT_H

    parts = [
        f'<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="Wind and solar capture rate by month" preserveAspectRatio="xMidYMid meet">',
        f'<text class="ax sm unit" x="{AXIS_LEFT - 5:.1f}" y="10" text-anchor="end">%</text>',
    ]
    for t in y_ticks:
        gy = y(t)
        parts.append(f'<line class="grid" x1="{AXIS_LEFT}" x2="{AXIS_LEFT + PLOT_W}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="ax sm" x="{AXIS_LEFT - 5:.1f}" y="{gy + 3.5:.1f}" text-anchor="end">{t:.0f}</text>')

    label_every = max(1, round(n / TARGET_X_LABELS))
    label_indices = list(range(0, n, label_every))
    if label_indices[-1] != n - 1:
        # The last regular-interval tick and the true final month can land
        # close enough together to overlap ("Feb 2026 Sep 2026" running
        # into each other) -- replace it with the final month rather than
        # showing both when they'd be closer than half a normal step apart.
        if n - 1 - label_indices[-1] < label_every / 2:
            label_indices[-1] = n - 1
        else:
            label_indices.append(n - 1)
    for i in label_indices:
        sx = x(i)
        parts.append(f'<line class="grid" x1="{sx:.1f}" x2="{sx:.1f}" y1="{PLOT_TOP}" y2="{PLOT_TOP + PLOT_H}"/>')
        parts.append(f'<text class="ax sm" x="{sx:.1f}" y="{X_LABEL_Y:.1f}" text-anchor="middle">{_month_label(months[i])}</text>')

    for by_month, color, label in ((wind_by, WIND_COLOR, "Wind"), (solar_by, SOLAR_COLOR, "Solar")):
        polylines, hits = _segments(months, by_month, x, y)
        for poly in polylines:
            parts.append(f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5"/>')
        for px, py, month, v in hits:
            # fill is set inline, not left to the .chart-hit CSS class, since
            # that class is scoped to body.live only (Live Market's dark
            # theme) -- this page has no such class, and an SVG <circle>
            # with no explicit fill defaults to solid black.
            parts.append(
                f'<circle class="chart-hit" cx="{px:.1f}" cy="{py:.1f}" r="{HIT_RADIUS}" fill="transparent">'
                f'<title>{label} {_month_label(month)}: {v:.1f}% capture rate</title></circle>'
            )

    parts.append("</svg>")
    return "".join(parts)
