"""
Inline SVG chart generation, matching the geometry of reference/recap-sketch.html.

Every function here takes plain data (as emitted by metrics.build_week) and
returns an SVG string. No template logic, no narrative -- purely pixel math,
mirroring the coordinates hand-picked in the sketch so real data lands in
the same visual system.
"""

from __future__ import annotations

import math

# ---- hero chart (day-ahead + imbalance across the week) -------------------

HERO_X0, HERO_X1 = 58.0, 986.0
HERO_Y_ZERO, HERO_Y_TOP = 196.0, 24.7  # pixel y for value=0 and value=axis_max
HERO_Y_AXIS_TOP, HERO_Y_AXIS_BOTTOM = 22, 196


def _nice_axis(max_val: float) -> tuple[float, float]:
    """Pick a round axis max and step giving ~4-6 gridlines above max_val."""
    if max_val <= 0:
        max_val = 10
    for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000):
        ticks = math.ceil(max_val / step)
        if 3 <= ticks <= 6:
            return step * ticks, step
    step = 1000
    return step * math.ceil(max_val / step), step


def hero_chart_svg(day_ahead: list[dict], imbalance: list[dict], days: list[dict]) -> str:
    all_vals = [r["value"] for r in day_ahead] + [r["value"] for r in imbalance]
    axis_max, step = _nice_axis(max(all_vals) if all_vals else 100)

    def y(v: float) -> float:
        return HERO_Y_ZERO - (v / axis_max) * (HERO_Y_ZERO - HERO_Y_TOP)

    n = len(day_ahead)
    span = HERO_X1 - HERO_X0

    def x(i: int) -> float:
        return HERO_X0 + (i / (n - 1)) * span if n > 1 else HERO_X0

    parts: list[str] = []

    # gridlines
    ticks = [step * k for k in range(0, int(axis_max / step) + 1)]
    for t in ticks:
        gy = y(t)
        parts.append(f'<line class="grid" x1="{HERO_X0}" x2="{HERO_X1}" y1="{gy:.1f}" y2="{gy:.1f}"/>')

    # day separators + axis day labels, from cumulative period counts
    cum = 0
    day_label_parts = []
    for d in days[:-1]:
        cum += d["periods"]
        sx = x(cum)  # boundary sits at the start index of the next day
        parts.append(f'<line class="daysep" x1="{sx:.1f}" x2="{sx:.1f}" y1="{HERO_Y_AXIS_TOP}" y2="{HERO_Y_AXIS_BOTTOM}"/>')

    cum = 0
    for d in days:
        start_i = cum
        end_i = cum + d["periods"] - 1
        mid_x = (x(start_i) + x(end_i)) / 2
        day_label_parts.append(f'<text class="ax" x="{mid_x:.1f}" y="216" text-anchor="middle">{d["short_label"]}</text>')
        cum += d["periods"]

    # imbalance polyline (drawn first, sits behind day-ahead)
    imb_points = " ".join(f"{x(i):.1f},{y(r['value']):.1f}" for i, r in enumerate(imbalance))
    parts.append(f'<polyline class="imb" points="{imb_points}"/>')

    # day-ahead polyline
    da_points = " ".join(f"{x(i):.1f},{y(r['value']):.1f}" for i, r in enumerate(day_ahead))
    parts.append(f'<polyline class="da" points="{da_points}"/>')

    # week-high imbalance marker
    peak_i = max(range(len(imbalance)), key=lambda i: imbalance[i]["value"])
    peak = imbalance[peak_i]
    mx, my = x(peak_i), y(peak["value"])
    parts.append(f'<circle class="mark" cx="{mx:.1f}" cy="{my:.1f}" r="4"/>')
    parts.append(f'<line class="markline" x1="{mx:.1f}" x2="{mx:.1f}" y1="{my + 6:.1f}" y2="{HERO_Y_AXIS_BOTTOM}"/>')
    label = f"£{peak['value']:.0f} imbalance, {peak['day_label']} SP{peak['sp']}"
    if mx > 780:
        parts.append(f'<text class="note" x="{mx - 10:.1f}" y="{my - 8:.1f}" text-anchor="end">{label}</text>')
    else:
        parts.append(f'<text class="note" x="{mx + 10:.1f}" y="{my - 8:.1f}" text-anchor="start">{label}</text>')

    # y-axis tick labels
    for t in ticks:
        gy = y(t)
        parts.append(f'<text class="ax" x="48" y="{gy + 4:.1f}" text-anchor="end">{t:g}</text>')
    parts.append('<text class="ax unit" x="48" y="16" text-anchor="end">£/MWh</text>')

    parts.extend(day_label_parts)

    body = "\n  ".join(parts)
    return (
        '<svg viewBox="0 0 1000 232" role="img" aria-labelledby="heroTitle" preserveAspectRatio="xMidYMid meet">\n'
        '  <title id="heroTitle">Half-hourly day-ahead and imbalance prices across the week, in pounds per megawatt hour</title>\n'
        f"  {body}\n"
        "</svg>"
    )


# ---- driver sparklines (wind / demand / periods-above-100) ----------------

SPARK_X0, SPARK_PITCH = 6.0, 48.0
SPARK_Y_TOP, SPARK_Y_BASE = 14.0, 74.0


def driver_sparkline_svg(values: list[float], dow_letters: list[str], css_class: str, aria_label: str) -> str:
    n = len(values)
    vmin, vmax = min(values), max(values)

    def x(i: int) -> float:
        return SPARK_X0 + i * SPARK_PITCH

    def y(v: float) -> float:
        if vmax == vmin:
            return (SPARK_Y_TOP + SPARK_Y_BASE) / 2
        return SPARK_Y_BASE - (v - vmin) / (vmax - vmin) * (SPARK_Y_BASE - SPARK_Y_TOP)

    pts = [(x(i), y(v)) for i, v in enumerate(values)]
    fill_points = f"6,{SPARK_Y_BASE:.0f} " + " ".join(f"{px:.1f},{py:.1f}" for px, py in pts) + f" {x(n - 1):.0f},{SPARK_Y_BASE:.0f}"
    line_points = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    dots = "".join(f'<circle class="dot {css_class}" cx="{px:.1f}" cy="{py:.1f}" r="2.5"/>' for px, py in pts)
    labels = "".join(
        f'<text class="ax sm" x="{px:.1f}" y="90" text-anchor="middle">{letter}</text>'
        for px, letter in zip((p[0] for p in pts), dow_letters)
    )
    return (
        f'<svg viewBox="0 0 300 96" role="img" aria-label="{aria_label}" preserveAspectRatio="xMidYMid meet">\n'
        f'  <polygon class="fill {css_class}" points="{fill_points}"/>\n'
        f'  <polyline class="line {css_class}" points="{line_points}"/>\n'
        f"  {dots}{labels}\n"
        "</svg>"
    )


# ---- heatmap: price relative to daily mean ---------------------------------

HM_X0, HM_Y0 = 46.0, 16.0
HM_CELL_W, HM_CELL_GAP, HM_CELL_H, HM_ROW_PITCH = 17.6, 0.6, 15.6, 17
HM_BLUE = (139, 167, 207)
HM_WHITE = (255, 255, 255)
HM_RED = (176, 76, 66)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _heatmap_color(delta: float, scale_max: float) -> str:
    t = min(abs(delta) / scale_max, 1.0) if scale_max else 0.0
    rgb = _lerp(HM_WHITE, HM_RED, t) if delta >= 0 else _lerp(HM_WHITE, HM_BLUE, t)
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def heatmap_svg(heatmap: list[dict], days: list[dict]) -> tuple[str, float]:
    max_periods = max(d["periods"] for d in days)
    deltas = [c["delta"] for c in heatmap]
    raw_max = max(abs(v) for v in deltas) if deltas else 1.0
    scale_max = math.ceil(raw_max / 5) * 5 or 5

    cells_by_day: dict[str, list[dict]] = {}
    for c in heatmap:
        cells_by_day.setdefault(c["sd"], []).append(c)

    parts = []
    for row_idx, d in enumerate(days):
        row_y = HM_Y0 + row_idx * HM_ROW_PITCH
        for c in cells_by_day.get(d["date"], []):
            cx = HM_X0 + (c["sp"] - 1) * (HM_CELL_W + HM_CELL_GAP)
            color = _heatmap_color(c["delta"], scale_max)
            sign = "+" if c["delta"] >= 0 else ""
            title = f"{d['short_label']} SP{c['sp']}: {sign}{c['delta']:.0f} £/MWh vs day mean"
            parts.append(
                f'<rect x="{cx:.1f}" y="{row_y:.0f}" width="{HM_CELL_W}" height="{HM_CELL_H}" '
                f'fill="{color}"><title>{title}</title></rect>'
            )

    row_label_y_offset = 12
    for row_idx, d in enumerate(days):
        row_y = HM_Y0 + row_idx * HM_ROW_PITCH + row_label_y_offset
        parts.append(f'<text class="ax sm" x="38" y="{row_y:.0f}" text-anchor="end">{DAY_NAMES[row_idx]}</text>')

    axis_y = HM_Y0 + len(days) * HM_ROW_PITCH + 30
    ticks = [1, *range(8, max_periods + 1, 8)]
    for sp in ticks:
        cx = HM_X0 + (sp - 1) * (HM_CELL_W + HM_CELL_GAP) + HM_CELL_W / 2
        parts.append(f'<text class="ax sm" x="{cx:.1f}" y="{axis_y - 18:.0f}" text-anchor="middle">{sp}</text>')
    mid_x = HM_X0 + (max_periods / 2) * (HM_CELL_W + HM_CELL_GAP)
    parts.append(f'<text class="ax sm" x="{mid_x:.1f}" y="{axis_y:.0f}" text-anchor="middle">Settlement period</text>')

    body = "\n  ".join(parts)
    view_h = axis_y + 4
    svg = (
        f'<svg viewBox="0 0 940 {view_h:.0f}" role="img" aria-label="Price relative to daily mean by settlement period and day" preserveAspectRatio="xMidYMid meet">\n'
        f"  {body}\n"
        "</svg>"
    )
    return svg, scale_max
