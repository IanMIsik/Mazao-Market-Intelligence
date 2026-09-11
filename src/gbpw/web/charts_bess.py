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


def service_type_colors(service_types: list[str]) -> dict[str, str]:
    return {svc: PALETTE[i % len(PALETTE)] for i, svc in enumerate(service_types)}


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

    n_ticks = int(round(axis_max / step))
    ticks = [step * k for k in range(0, n_ticks + 1)]
    for t in ticks:
        gx = x0 + (t / axis_max) * max_w
        parts.append(f'<line class="grid" x1="{gx:.1f}" x2="{gx:.1f}" y1="{top-4}" y2="{plot_bottom}"/>')
        parts.append(f'<text class="ax sm" x="{gx:.1f}" y="{plot_bottom+16}" text-anchor="middle">{t:,.0f}</text>')
    parts.append(f'<text class="ax unit sm" x="{x0+max_w+10:.1f}" y="{plot_bottom+16}" text-anchor="start">MW</text>')

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
