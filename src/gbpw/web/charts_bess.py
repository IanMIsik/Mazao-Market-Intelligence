"""
SVG geometry for the BESS Analytics charts. Same division of labour as
render/charts.py: pixel math happens here in Python, templates only embed
the returned markup. Not shared with render/charts.py -- different chart
family (horizontal bar / histogram / ranked bar vs. line/heatmap charts).

Colors are assigned by Python (inline fill), not CSS classes keyed to
service_type text -- service_type values are real strings with spaces
("Quick Reserve") and the set can grow, so a fixed palette list assigned in
order is simpler than generating CSS classes to match.
"""

from __future__ import annotations

import math
from html import escape

PALETTE = ["var(--navy)", "var(--navy-mid)", "var(--wind)", "var(--demand)", "var(--signal)"]

# A larger, hand-picked cycle for the revenue donut -- it can have 20+
# slices (one per participant with positive revenue that day), unlike
# PALETTE's 5 colors which are keyed to the small, fixed service_type set.
# Colors repeat past this length; only the top 8 (by revenue) get an
# explicit legend entry, so tail-slice color reuse doesn't cost legibility
# -- every slice still identifies itself via its hover title regardless.
DONUT_PALETTE = [
    "#10294A", "#2B4B73", "#5A8F6E", "#8C7BA6", "#B04A39",
    "#3E7C8C", "#C98A3B", "#6B5B95", "#4E7A51", "#A15C7A",
    "#5C6BC0", "#7C9C4C",
]


def service_type_colors(service_types: list[str]) -> dict[str, str]:
    return {svc: PALETTE[i % len(PALETTE)] for i, svc in enumerate(service_types)}


def donut_colors(n: int) -> list[str]:
    return [DONUT_PALETTE[i % len(DONUT_PALETTE)] for i in range(n)]


def _nice_axis(max_val: float, target_ticks: int = 5) -> tuple[float, float]:
    """Pick a round axis max and step (a 1/2/2.5/5 x 10^n 'nice number')
    giving ~target_ticks gridlines above max_val. Same idea as
    render/charts.py's _nice_axis, generalized to any magnitude since
    cleared MW totals here range from tens to millions depending on window
    and service type -- not reused directly since that one is hardcoded to
    a handful of £/MWh-sized steps.
    """
    if max_val <= 0:
        return 10.0, 2.0
    raw_step = max_val / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = magnitude
    for m in (1, 2, 2.5, 5, 10):
        step = m * magnitude
        if step >= raw_step:
            break
    ticks = math.ceil(max_val / step)
    return step * ticks, step


def market_summary_bars_svg(by_service_type: list[dict]) -> str:
    """Horizontal bar per service_type, total cleared MW, against a shared
    MW axis scale (gridlines + tick labels) so bar lengths are calibrated
    to real MW values, not just proportional to the tallest bar.
    """
    if not by_service_type:
        return ""
    colors = service_type_colors([r["service_type"] for r in by_service_type])
    max_val = max(r["cleared_mw"] for r in by_service_type) or 1.0
    axis_max, step = _nice_axis(max_val)

    x0, max_w, row_h, bar_h, top = 190, 650, 46, 26, 16
    plot_bottom = top + len(by_service_type) * row_h
    height = plot_bottom + 34
    parts = [f'<svg viewBox="0 0 900 {height}" role="img" '
             f'aria-label="Total cleared MW by service type, with MW axis scale" preserveAspectRatio="xMidYMid meet">']

    # "MW" sits once, top-right above the plot area (same placement idea as
    # the GB Power Weekly hero chart's "£/MWh" label) -- not inline after
    # the last tick number, which overlapped it once axis_max ran into the
    # millions (a 7-8 digit tick label like "8,000,000" is wide enough to
    # collide with anything placed right after it on the same baseline).
    parts.append(f'<text class="ax unit sm" x="{x0+max_w:.1f}" y="{top-6}" text-anchor="end">MW</text>')

    n_ticks = int(round(axis_max / step))
    ticks = [step * k for k in range(0, n_ticks + 1)]
    for i, t in enumerate(ticks):
        gx = x0 + (t / axis_max) * max_w
        # The last tick would otherwise center itself half off the right
        # edge of the axis; anchoring it to "end" keeps it fully inside
        # the plot area instead of overhanging into empty margin.
        anchor = "end" if i == len(ticks) - 1 else "middle"
        parts.append(f'<line class="grid" x1="{gx:.1f}" x2="{gx:.1f}" y1="{top-4}" y2="{plot_bottom}"/>')
        parts.append(f'<text class="ax sm" x="{gx:.1f}" y="{plot_bottom+16}" text-anchor="{anchor}">{t:,.0f}</text>')

    for i, r in enumerate(by_service_type):
        y = top + i * row_h
        cy = y + bar_h / 2
        w = (r["cleared_mw"] / axis_max) * max_w
        label = escape(r["service_type"])
        color = colors[r["service_type"]]
        parts.append(f'<text class="ax" x="{x0-12}" y="{cy+4:.1f}" text-anchor="end">{label}</text>')
        parts.append(
            f'<rect fill="{color}" x="{x0}" y="{y}" width="{w:.1f}" height="{bar_h}">'
            f'<title>{label}: {r["cleared_mw"]:,.1f} MW, {r["participants"]} participant(s)</title></rect>'
        )
        parts.append(f'<text class="ax num" x="{x0+w+10:.1f}" y="{cy+4:.1f}" text-anchor="start">{r["cleared_mw"]:,.1f} MW</text>')
    parts.append("</svg>")
    return "".join(parts)


DONUT_LABEL_MIN_FRACTION = 0.04  # slices smaller than this get a tooltip only -- their own % wouldn't fit


def daily_revenue_donut_svg(slices: list[dict]) -> str:
    """Donut chart, one segment per participant, sized by share of total
    revenue among the participants passed in. Callers filter out
    non-positive-revenue participants first (see routes_bess.py) --
    revenue can be genuinely negative (a unit paying to provide response),
    and a donut can't represent a negative share of a whole, so those are
    disclosed separately rather than distorting the chart.

    Built from stacked <circle> strokes (the standard SVG donut trick:
    stroke-dasharray splits each circle's outline into one visible arc
    plus one fully-transparent remainder, stroke-dashoffset shifts where
    that arc starts), not one <path> per wedge -- simpler arithmetic, no
    arc-flag edge cases at 0%/100%.

    Every slice carries data-* attributes (participant, revenue, avg
    clearing price, cleared MW, share) so the page's own click handler
    (bess_analytics.html) can drive the KPI cards and the selected-name
    label client-side, without a server round-trip per click. That label
    lives in plain HTML below the chart now, not as SVG text in the
    donut's hole -- a long company name never fit that small a circle
    without wrapping or overflowing it. Slices at or above
    DONUT_LABEL_MIN_FRACTION also get a plain-upright percentage label
    drawn on the ring -- placed outside the rotated <g> with hand-computed
    coordinates (clockwise-from-12-o'clock, matching the group's own
    rotate(-90) so the two line up) rather than inside it, so the text
    itself never ends up sideways or upside-down for slices past 3/6/9
    o'clock.
    """
    if not slices:
        return ""
    total = sum(r["revenue_gbp"] for r in slices) or 1.0
    colors = donut_colors(len(slices))

    # cx/cy are derived from the ring's own outer edge (radius + half the
    # stroke width) plus a small margin, and set equal to half the
    # viewBox -- not picked independently -- so the ring sits truly
    # centered in its box. It previously wasn't: cx/cy were chosen
    # separately from the size formula, leaving a large, unintended blank
    # margin on one side that no amount of CSS gap tuning around the
    # <svg> could fix, since the whitespace was baked into the viewBox.
    radius, stroke_w = 72, 46
    cx = cy = radius + stroke_w / 2 + 6
    circumference = 2 * math.pi * radius
    size = cx * 2

    parts = [
        f'<svg id="revenue-donut" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="Today\'s EAC revenue share by participant" preserveAspectRatio="xMidYMid meet">',
        f'<g transform="rotate(-90 {cx} {cy})">',
    ]
    labels: list[str] = []

    cumulative = 0.0
    for i, r in enumerate(slices):
        frac = r["revenue_gbp"] / total
        arc = frac * circumference
        avg_price = r["avg_clearing_price"]
        avg_price_attr = f"{avg_price:.4f}" if avg_price is not None else ""
        label = escape(r["participant"])
        parts.append(
            f'<circle class="donut-slice" cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
            f'stroke="{colors[i]}" stroke-width="{stroke_w}" '
            f'stroke-dasharray="{arc:.2f} {circumference - arc:.2f}" stroke-dashoffset="{-cumulative:.2f}" '
            f'data-participant="{label}" data-revenue="{r["revenue_gbp"]:.2f}" '
            f'data-avg-price="{avg_price_attr}" data-mw="{r["cleared_mw"]:.2f}" data-pct="{frac * 100:.1f}">'
            f'<title>{label}: £{r["revenue_gbp"]:,.0f} ({frac * 100:.1f}% of today\'s revenue)</title>'
            f"</circle>"
        )
        if frac >= DONUT_LABEL_MIN_FRACTION:
            clock_deg = (cumulative + arc / 2) / circumference * 360
            clock_rad = math.radians(clock_deg)
            lx = cx + radius * math.sin(clock_rad)
            ly = cy - radius * math.cos(clock_rad)
            labels.append(
                f'<text class="donut-pct" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                f'dominant-baseline="central">{frac * 100:.0f}%</text>'
            )
        cumulative += arc

    parts.append("</g>")
    parts.extend(labels)
    parts.append("</svg>")
    return "".join(parts)


def distribution_histogram_svg(dist: dict) -> str:
    """Vertical bar per bucket, count of participants."""
    buckets = dist["buckets"]
    if not buckets:
        return ""
    max_count = max(b["count"] for b in buckets) or 1

    x0, plot_w, plot_h, gap, top = 50, 820, 200, 10, 20
    n = len(buckets)
    bar_w = (plot_w - gap * (n - 1)) / n
    height = plot_h + 70
    parts = [f'<svg viewBox="0 0 {x0+plot_w+20} {height}" role="img" '
             f'aria-label="Distribution of total cleared MW per participant" preserveAspectRatio="xMidYMid meet">']
    for i, b in enumerate(buckets):
        h = (b["count"] / max_count) * plot_h
        x = x0 + i * (bar_w + gap)
        y = plot_h - h + top
        cx = x + bar_w / 2
        label = f'{b["low_mw"]:,.0f}–{b["high_mw"]:,.0f}' if b["high_mw"] is not None else f'{b["low_mw"]:,.0f}+'
        parts.append(
            f'<rect class="histbar" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}">'
            f'<title>{label} MW: {b["count"]} participant(s)</title></rect>'
        )
        parts.append(f'<text class="ax num" x="{cx:.1f}" y="{y-6:.1f}" text-anchor="middle">{b["count"]}</text>')
        parts.append(f'<text class="ax sm" x="{cx:.1f}" y="{plot_h+top+18:.1f}" text-anchor="middle">{label}</text>')
    parts.append(f'<line class="axline" x1="{x0}" x2="{x0+plot_w}" y1="{plot_h+top}" y2="{plot_h+top}"/>')
    parts.append("</svg>")
    return "".join(parts)


def leaderboard_bars_svg(leaderboard: list[dict]) -> str:
    """Ranked horizontal bars, £/MW/day (BM leaderboard, units with known capacity)."""
    if not leaderboard:
        return ""
    max_val = max(r["gbp_per_mw_per_day"] for r in leaderboard) or 1.0

    x0, max_w, row_h, bar_h, top = 150, 620, 32, 18, 16
    height = top * 2 + len(leaderboard) * row_h
    parts = [f'<svg viewBox="0 0 900 {height}" role="img" '
             f'aria-label="Top units ranked by pounds per megawatt per day" preserveAspectRatio="xMidYMid meet">']
    for i, r in enumerate(leaderboard):
        y = top + i * row_h
        cy = y + bar_h / 2
        val = r["gbp_per_mw_per_day"]
        w = (val / max_val) * max_w
        label = escape(r["national_grid_bm_unit"])
        parts.append(f'<text class="ax" x="{x0-10}" y="{cy+4:.1f}" text-anchor="end">{label}</text>')
        parts.append(
            f'<rect class="rankbar" x="{x0}" y="{y}" width="{w:.1f}" height="{bar_h}">'
            f'<title>{label}: £{val:,.2f}/MW/day</title></rect>'
        )
        parts.append(f'<text class="ax num" x="{x0+w+8:.1f}" y="{cy+4:.1f}" text-anchor="start">£{val:,.2f}/MW/day</text>')
    parts.append("</svg>")
    return "".join(parts)
