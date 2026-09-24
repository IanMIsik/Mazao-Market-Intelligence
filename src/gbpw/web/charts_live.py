"""
SVG geometry for the Live Market page. Own module, not shared with
charts_bess.py or render/charts.py -- different chart family (a single or
paired today-so-far progression per dataset), but the axis conventions
(_ax_ticks-style "nice" gridlines, .ax/.grid/.axline CSS classes) follow
render/charts.py's hero chart rather than inventing a new visual language.

Unlike the weekly report's hero chart, these charts can't assume a
0-based y-axis -- interconnector flow is signed (+import/-export), wind/
solar/demand/price are not -- so the axis is built from the data's actual
[min, max] rather than always starting at 0.

Hover values (per settlement period) use plain SVG <title> elements on an
invisible per-point hit-circle -- the same native-tooltip mechanism
render/charts.py's heatmap_svg() already uses for its cells -- rather than
adding a JS charting layer to a project that otherwise has none.
"""

from __future__ import annotations

import json
import math
from datetime import date

AXIS_LEFT = 38.0
PLOT_W = 214.0
PLOT_TOP = 18.0  # headroom above the top gridline so the unit label (drawn
                 # above the plot) never overlaps that gridline's own value
                 # label -- they used to collide when PLOT_TOP was small.
PLOT_H = 100.0  # tall enough that the rendered chart actually fills its
                # card's height instead of leaving a lot of unused space
                # below it -- a card's height is mostly set by its title/
                # subtitle/legend text, not by a short chart.
UNIT_LABEL_Y = 8.0
X_LABEL_Y = PLOT_TOP + PLOT_H + 13
CAPTION_Y = X_LABEL_Y + 13
VIEW_W = AXIS_LEFT + PLOT_W + 8
VIEW_H = CAPTION_Y + 6
HIT_RADIUS = 4.5

AXIS_RIGHT = 42.0  # mirrors AXIS_LEFT, for dual_series_svg()'s second axis
DUAL_VIEW_W = AXIS_LEFT + PLOT_W + AXIS_RIGHT


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
    """Round gridline values spanning at least [min_v, max_v]. Always
    includes 0 as an exact tick when the data straddles zero (interconnector
    flow), since that's the import/export reference line, not just another
    number on the scale.
    """
    if min_v == max_v:
        min_v, max_v = min_v - 1, max_v + 1
    step = _nice_step(max_v - min_v, target_ticks)
    lo = math.floor(min_v / step) * step
    hi = math.ceil(max_v / step) * step
    ticks = []
    t = lo
    while t <= hi + step / 2 and len(ticks) < 12:
        ticks.append(round(t, 6))
        t += step
    return ticks


def _sp_ticks(min_sp: int, max_sp: int, target_ticks: int = 6) -> list[int]:
    span = max_sp - min_sp
    if span <= 0:
        return [min_sp]
    step = max(2, round(span / target_ticks / 2) * 2)  # even step -- lands on whole hours
    ticks = list(range(min_sp, max_sp, step))
    if not ticks or ticks[-1] != max_sp:
        ticks.append(max_sp)
    return ticks


def _fmt(v: float) -> str:
    return f"{v:g}"


def _axes(values: list[float], sps: list[int], unit_label: str) -> tuple[dict, list[str]]:
    min_v, max_v = min(values), max(values)
    y_ticks = _y_ticks(min_v, max_v)
    axis_min, axis_max = y_ticks[0], y_ticks[-1]
    v_span = (axis_max - axis_min) or 1.0

    min_sp, max_sp = min(sps), max(sps)
    sp_span = (max_sp - min_sp) or 1

    def x(sp: int) -> float:
        return AXIS_LEFT + ((sp - min_sp) / sp_span) * PLOT_W

    def y(v: float) -> float:
        return PLOT_TOP + PLOT_H - ((v - axis_min) / v_span) * PLOT_H

    parts = [f'<text class="ax sm unit" x="{AXIS_LEFT - 5:.1f}" y="{UNIT_LABEL_Y:.1f}" text-anchor="end">{unit_label}</text>']
    for t in y_ticks:
        gy = y(t)
        line_class = "axline" if t == 0 and axis_min < 0 < axis_max else "grid"
        parts.append(f'<line class="{line_class}" x1="{AXIS_LEFT}" x2="{AXIS_LEFT + PLOT_W}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="ax sm" x="{AXIS_LEFT - 5:.1f}" y="{gy + 3.5:.1f}" text-anchor="end">{_fmt(t)}</text>')

    for sp in _sp_ticks(min_sp, max_sp):
        sx = x(sp)
        parts.append(f'<line class="grid" x1="{sx:.1f}" x2="{sx:.1f}" y1="{PLOT_TOP}" y2="{PLOT_TOP + PLOT_H}"/>')
        parts.append(f'<text class="ax sm" x="{sx:.1f}" y="{X_LABEL_Y:.1f}" text-anchor="middle">{sp}</text>')
    parts.append(f'<text class="ax sm" x="{AXIS_LEFT + PLOT_W / 2:.1f}" y="{CAPTION_Y:.1f}" text-anchor="middle">Settlement period</text>')

    return {"x": x, "y": y}, parts


def _dual_axes(
    left_values: list[float], right_values: list[float], sps: list[int], left_unit: str, right_unit: str
) -> tuple[dict, list[str]]:
    """Two independent y-scales sharing one x-axis -- left for one series,
    right (mirrored, ticks on the outside edge) for the other. Used when
    two series don't share a unit and a single shared scale would flatten
    one of them (see dual_series_svg()); render/charts.py's hero chart and
    this module's own _axes() both only ever handle one shared scale.
    """
    left_min, left_max = min(left_values), max(left_values)
    right_min, right_max = min(right_values), max(right_values)
    left_ticks = _y_ticks(left_min, left_max)
    right_ticks = _y_ticks(right_min, right_max)
    left_axis_min, left_axis_max = left_ticks[0], left_ticks[-1]
    right_axis_min, right_axis_max = right_ticks[0], right_ticks[-1]
    left_span = (left_axis_max - left_axis_min) or 1.0
    right_span = (right_axis_max - right_axis_min) or 1.0

    min_sp, max_sp = min(sps), max(sps)
    sp_span = (max_sp - min_sp) or 1
    plot_right = AXIS_LEFT + PLOT_W

    def x(sp: int) -> float:
        return AXIS_LEFT + ((sp - min_sp) / sp_span) * PLOT_W

    def y_left(v: float) -> float:
        return PLOT_TOP + PLOT_H - ((v - left_axis_min) / left_span) * PLOT_H

    def y_right(v: float) -> float:
        return PLOT_TOP + PLOT_H - ((v - right_axis_min) / right_span) * PLOT_H

    parts = [f'<text class="ax sm unit" x="{AXIS_LEFT - 5:.1f}" y="{UNIT_LABEL_Y:.1f}" text-anchor="end">{left_unit}</text>']
    for t in left_ticks:
        gy = y_left(t)
        line_class = "axline" if t == 0 and left_axis_min < 0 < left_axis_max else "grid"
        parts.append(f'<line class="{line_class}" x1="{AXIS_LEFT}" x2="{plot_right}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="ax sm" x="{AXIS_LEFT - 5:.1f}" y="{gy + 3.5:.1f}" text-anchor="end">{_fmt(t)}</text>')

    parts.append(f'<text class="ax sm unit" x="{plot_right + 5:.1f}" y="{UNIT_LABEL_Y:.1f}" text-anchor="start">{right_unit}</text>')
    for t in right_ticks:
        parts.append(f'<text class="ax sm" x="{plot_right + 5:.1f}" y="{y_right(t) + 3.5:.1f}" text-anchor="start">{_fmt(t)}</text>')

    for sp in _sp_ticks(min_sp, max_sp):
        sx = x(sp)
        parts.append(f'<line class="grid" x1="{sx:.1f}" x2="{sx:.1f}" y1="{PLOT_TOP}" y2="{PLOT_TOP + PLOT_H}"/>')
        parts.append(f'<text class="ax sm" x="{sx:.1f}" y="{X_LABEL_Y:.1f}" text-anchor="middle">{sp}</text>')
    parts.append(f'<text class="ax sm" x="{AXIS_LEFT + PLOT_W / 2:.1f}" y="{CAPTION_Y:.1f}" text-anchor="middle">Settlement period</text>')

    return {"x": x, "y_left": y_left, "y_right": y_right}, parts


def progression_svg(points: list[dict], color: str, unit_label: str) -> str:
    """A single line across today's settlement periods so far, with a
    scaled y-axis, settlement-period tick marks on the x-axis, and a
    hoverable value tooltip at each point. Empty (nothing cleared/
    published yet today) returns "" -- the template shows its own
    empty-state text instead, same honesty convention as every other chart
    on this site.
    """
    if not points:
        return ""
    values = [p["value"] for p in points]
    sps = [p["sp"] for p in points]
    axes, axis_parts = _axes(values, sps, unit_label)
    x, y = axes["x"], axes["y"]

    coords = [(x(p["sp"]), y(p["value"])) for p in points]
    poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    last_x, last_y = coords[-1]

    hits = "".join(
        f'<circle class="chart-hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{HIT_RADIUS}">'
        f'<title>SP{p["sp"]}: {_fmt(p["value"])} {unit_label}</title></circle>'
        for (cx, cy), p in zip(coords, points)
    )

    parts = [
        f'<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="Today so far, {unit_label}" preserveAspectRatio="xMidYMid meet">',
        *axis_parts,
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1"/>',
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{color}"/>',
        hits,
        "</svg>",
    ]
    return "".join(parts)


def multi_day_forecast_svg(points: list[dict], color: str, unit_label: str) -> str:
    """A single line across a multi-day window (the Forecasts page's "all
    14 days" view) -- points carry {date, sp, value}, chronologically
    ordered, potentially spanning hundreds of settlement periods across
    many calendar days, unlike every other chart in this module (all
    scoped to one settlement day). x is a plain sequential index across
    every point rather than a settlement-period value, since sp alone
    repeats every day and can't place a point in time on its own; a
    gridline + short date label marks each day's first point instead of
    _axes()'s per-settlement-period ticks.

    No per-point hover circles (every other chart here has them) -- at up
    to 14 x 48 points across a ~214px plot, individual points sit closer
    together than chart-hit's own hit-radius, so a hit-circle per point
    would just always show whichever one happens to be on top, not
    meaningfully "the point you're pointing at". Instead the full point
    array + the exact axis bounds used here are embedded as data-*
    attributes on the <svg> root; forecasts.html's own script finds the
    *nearest* point to the cursor's x position (not a fixed hit-target)
    and moves one shared crosshair/dot/tooltip to it -- one JS-driven
    hover for the whole line instead of hundreds of static hit-circles.
    axis_min/axis_max (the same _y_ticks()-rounded bounds the polyline
    itself is drawn against, not the data's raw min/max) are included
    specifically so that JS-computed dot lands exactly on the line, not
    slightly off from using a different y-scale than the SVG was drawn
    with.
    """
    if not points:
        return ""
    values = [p["value"] for p in points]
    min_v, max_v = min(values), max(values)
    y_ticks = _y_ticks(min_v, max_v)
    axis_min, axis_max = y_ticks[0], y_ticks[-1]
    v_span = (axis_max - axis_min) or 1.0

    n = len(points)
    span = (n - 1) or 1

    def x(i: int) -> float:
        return AXIS_LEFT + (i / span) * PLOT_W

    def y(v: float) -> float:
        return PLOT_TOP + PLOT_H - ((v - axis_min) / v_span) * PLOT_H

    axis_parts = [f'<text class="ax sm unit" x="{AXIS_LEFT - 5:.1f}" y="{UNIT_LABEL_Y:.1f}" text-anchor="end">{unit_label}</text>']
    for t in y_ticks:
        gy = y(t)
        line_class = "axline" if t == 0 and axis_min < 0 < axis_max else "grid"
        axis_parts.append(f'<line class="{line_class}" x1="{AXIS_LEFT}" x2="{AXIS_LEFT + PLOT_W}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        axis_parts.append(f'<text class="ax sm" x="{AXIS_LEFT - 5:.1f}" y="{gy + 3.5:.1f}" text-anchor="end">{_fmt(t)}</text>')

    day_starts: list[tuple[int, str]] = []
    last_date = None
    for i, p in enumerate(points):
        if p["date"] == last_date:
            continue
        last_date = p["date"]
        day_starts.append((i, p["date"]))

    # A gridline at every day boundary, but a text label only every Nth
    # one -- up to 14 of them (one per day) crammed into a ~214px plot
    # would overlap into an unreadable smear at any legible font size
    # (same lesson as the imbalance-by-SP table earlier), so labels are
    # thinned to a target count the same way _sp_ticks() already thins
    # settlement-period ticks, while every gridline stays (thin, light --
    # more of them costs nothing the way overlapping text does).
    target_labels = 7
    label_step = max(1, round(len(day_starts) / target_labels))
    last_j = len(day_starts) - 1
    last_shown_j = None
    for j, (i, iso_date) in enumerate(day_starts):
        sx = x(i)
        axis_parts.append(f'<line class="grid" x1="{sx:.1f}" x2="{sx:.1f}" y1="{PLOT_TOP}" y2="{PLOT_TOP + PLOT_H}"/>')
        # The final day always gets a label (so the window's own end is
        # never ambiguous), but only if it isn't already about to collide
        # with the label right before it -- otherwise skip the regular
        # step's pick for one that's too close to the forced last one.
        is_regular_step = j % label_step == 0
        is_forced_last = j == last_j and (last_shown_j is None or j - last_shown_j >= label_step)
        if not (is_regular_step or is_forced_last):
            continue
        last_shown_j = j
        # "%-d" (no leading zero) isn't portable -- glibc supports it,
        # Windows' CRT doesn't (confirmed: this app's own dev server runs
        # on Windows). Build the label manually instead.
        d = date.fromisoformat(iso_date)
        label = f"{d.day} {d.strftime('%b')}"
        axis_parts.append(f'<text class="ax sm" x="{sx:.1f}" y="{X_LABEL_Y:.1f}" text-anchor="middle">{label}</text>')

    coords = [(x(i), y(p["value"])) for i, p in enumerate(points)]
    poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)

    # Single-quoted so the JSON's own double-quoted strings don't need
    # escaping; HTML permits either quote style on an attribute.
    point_data = json.dumps([{"date": p["date"], "sp": p["sp"], "value": p["value"]} for p in points])

    parts = [
        f'<svg class="chart-multiday" viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="14-day forecast, {unit_label}" preserveAspectRatio="xMidYMid meet" '
        f"data-points='{point_data}' data-axis-left=\"{AXIS_LEFT}\" data-plot-w=\"{PLOT_W}\" "
        f'data-plot-top="{PLOT_TOP}" data-plot-h="{PLOT_H}" data-axis-min="{axis_min}" '
        f'data-axis-max="{axis_max}" data-unit="{unit_label}">',
        *axis_parts,
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1"/>',
        f'<line class="chart-crosshair" x1="0" x2="0" y1="{PLOT_TOP}" y2="{PLOT_TOP + PLOT_H}" style="display:none;"/>',
        f'<circle class="chart-crosshair-dot" r="3" fill="{color}" style="display:none;"/>',
        "</svg>",
    ]
    return "".join(parts)


def comparison_svg(points: list[dict], actual_color: str, forecast_color: str, unit_label: str) -> str:
    """Today's actual (solid) against the latest-published forecast
    (dashed) across settlement periods, with the same scaled axes and
    per-point hover tooltips as progression_svg(). Shape matches
    live_market_metrics.actual_vs_forecast(): each point has
    {sp, actual, forecast}, either of which may be None for a period that
    hasn't cleared/published yet. Empty (neither side has any data at all)
    returns "" -- same empty-state convention as progression_svg().
    """
    if not points:
        return ""
    values = [p[key] for p in points for key in ("actual", "forecast") if p[key] is not None]
    if not values:
        return ""
    sps = [p["sp"] for p in points]
    axes, axis_parts = _axes(values, sps, unit_label)
    x, y = axes["x"], axes["y"]

    def line(key: str, color: str, dashed: bool) -> str:
        coords = [(x(p["sp"]), y(p[key])) for p in points if p[key] is not None]
        if len(coords) < 2:
            return ""
        poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
        dash = ' stroke-dasharray="4,3"' if dashed else ""
        return f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1"{dash}/>'

    forecast_line = line("forecast", forecast_color, dashed=True)
    actual_line = line("actual", actual_color, dashed=False)

    marker = ""
    last_actual = next((p for p in reversed(points) if p["actual"] is not None), None)
    if last_actual is not None:
        cx, cy = x(last_actual["sp"]), y(last_actual["actual"])
        marker = f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.5" fill="{actual_color}"/>'

    def hit_title(p: dict) -> str:
        bits = []
        if p["actual"] is not None:
            bits.append(f"Actual {_fmt(p['actual'])} {unit_label}")
        if p["forecast"] is not None:
            bits.append(f"Forecast {_fmt(p['forecast'])} {unit_label}")
        return f"SP{p['sp']}: " + ", ".join(bits)

    # One hit-circle per point, positioned on whichever series has a value
    # (preferring actual) -- avoids drawing two overlapping hit-targets at
    # slightly different y positions for the same settlement period.
    hits = []
    for p in points:
        ref_key = "actual" if p["actual"] is not None else "forecast"
        if p[ref_key] is None:
            continue
        cx, cy = x(p["sp"]), y(p[ref_key])
        hits.append(f'<circle class="chart-hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{HIT_RADIUS}"><title>{hit_title(p)}</title></circle>')

    parts = [
        f'<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="Actual vs forecast, {unit_label}" preserveAspectRatio="xMidYMid meet">',
        *axis_parts,
        forecast_line,
        actual_line,
        marker,
        *hits,
        "</svg>",
    ]
    return "".join(p for p in parts if p)


def triple_comparison_svg(
    points: list[dict], actual_color: str, combined_color: str, forecast_color: str, unit_label: str
) -> str:
    """Three lines on one chart: actual (solid), actual+addon (solid, a
    second color -- e.g. wind + curtailed volume, see
    live_market_metrics.actual_and_addon_vs_forecast()), and forecast
    (dashed). combined lags actual by however long its addon takes to
    publish, so the two solid lines will often end at different
    settlement periods -- that's real, not a rendering gap, and each
    line's own last point gets its own end-of-line marker so it reads
    clearly rather than looking like one broken series.
    """
    if not points:
        return ""
    values = [p[key] for p in points for key in ("actual", "combined", "forecast") if p[key] is not None]
    if not values:
        return ""
    sps = [p["sp"] for p in points]
    axes, axis_parts = _axes(values, sps, unit_label)
    x, y = axes["x"], axes["y"]

    def line(key: str, color: str, dashed: bool) -> str:
        coords = [(x(p["sp"]), y(p[key])) for p in points if p[key] is not None]
        if len(coords) < 2:
            return ""
        poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
        dash = ' stroke-dasharray="4,3"' if dashed else ""
        return f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1"{dash}/>'

    forecast_line = line("forecast", forecast_color, dashed=True)
    actual_line = line("actual", actual_color, dashed=False)
    combined_line = line("combined", combined_color, dashed=False)

    markers = []
    for key, color in (("actual", actual_color), ("combined", combined_color)):
        last = next((p for p in reversed(points) if p[key] is not None), None)
        if last is not None:
            mx, my = x(last["sp"]), y(last[key])
            markers.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="2.5" fill="{color}"/>')

    def hit_title(p: dict) -> str:
        bits = []
        if p["actual"] is not None:
            bits.append(f"Actual {_fmt(p['actual'])} {unit_label}")
        if p["combined"] is not None:
            bits.append(f"Actual+curtailed {_fmt(p['combined'])} {unit_label}")
        if p["forecast"] is not None:
            bits.append(f"Forecast {_fmt(p['forecast'])} {unit_label}")
        return f"SP{p['sp']}: " + ", ".join(bits)

    hits = []
    for p in points:
        ref_key = "combined" if p["combined"] is not None else ("actual" if p["actual"] is not None else None)
        if ref_key is None:
            continue
        cx, cy = x(p["sp"]), y(p[ref_key])
        hits.append(f'<circle class="chart-hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{HIT_RADIUS}"><title>{hit_title(p)}</title></circle>')

    parts = [
        f'<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="Actual, actual plus curtailed, and forecast, {unit_label}" preserveAspectRatio="xMidYMid meet">',
        *axis_parts,
        forecast_line,
        actual_line,
        combined_line,
        *markers,
        *hits,
        "</svg>",
    ]
    return "".join(p for p in parts if p)


def dual_series_svg(
    points: list[dict], a_color: str, b_color: str, a_unit: str, b_unit: str
) -> str:
    """Two lines, independent left/right y-scales, sharing one x-axis --
    for two series that don't share a unit (e.g. imbalance price £/MWh on
    the left, net imbalance volume MWh on the right) where forcing them
    onto one shared scale would flatten whichever has the smaller range
    into a meaningless near-flat line. Shape matches
    live_market_metrics.dual_series_today(): each point has {sp, a, b},
    either of which may be None for a period one series hasn't cleared
    for yet -- neither side is treated as secondary the way forecast is
    in comparison_svg(), both are independently live actuals.
    """
    if not points:
        return ""
    a_values = [p["a"] for p in points if p["a"] is not None]
    b_values = [p["b"] for p in points if p["b"] is not None]
    if not a_values or not b_values:
        return ""
    sps = [p["sp"] for p in points]
    axes, axis_parts = _dual_axes(a_values, b_values, sps, a_unit, b_unit)
    x = axes["x"]

    def line(key: str, y_fn, color: str) -> str:
        coords = [(x(p["sp"]), y_fn(p[key])) for p in points if p[key] is not None]
        if len(coords) < 2:
            return ""
        poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
        return f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1"/>'

    a_line = line("a", axes["y_left"], a_color)
    b_line = line("b", axes["y_right"], b_color)

    markers = []
    for key, y_fn, color in (("a", axes["y_left"], a_color), ("b", axes["y_right"], b_color)):
        last = next((p for p in reversed(points) if p[key] is not None), None)
        if last is not None:
            mx, my = x(last["sp"]), y_fn(last[key])
            markers.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="2.5" fill="{color}"/>')

    def hit_title(p: dict) -> str:
        bits = []
        if p["a"] is not None:
            bits.append(f"{_fmt(p['a'])} {a_unit}")
        if p["b"] is not None:
            bits.append(f"{_fmt(p['b'])} {b_unit}")
        return f"SP{p['sp']}: " + ", ".join(bits)

    hits = []
    for p in points:
        if p["a"] is None and p["b"] is None:
            continue
        ref_key, y_fn = ("a", axes["y_left"]) if p["a"] is not None else ("b", axes["y_right"])
        cx, cy = x(p["sp"]), y_fn(p[ref_key])
        hits.append(f'<circle class="chart-hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{HIT_RADIUS}"><title>{hit_title(p)}</title></circle>')

    parts = [
        f'<svg viewBox="0 0 {DUAL_VIEW_W:.0f} {VIEW_H:.0f}" role="img" '
        f'aria-label="Two series, {a_unit} and {b_unit}, on independent scales" preserveAspectRatio="xMidYMid meet">',
        *axis_parts,
        a_line,
        b_line,
        *markers,
        *hits,
        "</svg>",
    ]
    return "".join(p for p in parts if p)
