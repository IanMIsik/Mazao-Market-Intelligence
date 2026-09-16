"""
SVG geometry for the Live Market page. Own module, not shared with
charts_bess.py or render/charts.py -- different chart family (a single
today-so-far progression line per dataset), same division of labour
(pixel math here, templates only embed the returned markup).
"""

from __future__ import annotations


def progression_svg(points: list[dict], color: str, unit_label: str) -> str:
    """A single line across today's settlement periods so far. Empty
    (nothing cleared/published yet today) returns "" -- the template shows
    its own empty-state text instead, same honesty convention as every
    other chart on this site.
    """
    if not points:
        return ""
    values = [p["value"] for p in points]
    min_v, max_v = min(values), max(values)
    span = (max_v - min_v) or 1.0

    x0, plot_w, plot_h, top = 0, 240, 60, 4
    n = len(points)

    def x(i: int) -> float:
        return x0 + (i / (n - 1)) * plot_w if n > 1 else x0

    def y(v: float) -> float:
        return top + plot_h - ((v - min_v) / span) * plot_h

    coords = [(x(i), y(p["value"])) for i, p in enumerate(points)]
    poly = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    last_x, last_y = coords[-1]

    parts = [
        f'<svg viewBox="0 0 240 {plot_h + top * 2}" role="img" '
        f'aria-label="Today so far, {unit_label}" preserveAspectRatio="xMidYMid meet">',
        f'<line class="grid" x1="0" x2="240" y1="{top + plot_h}" y2="{top + plot_h}"/>',
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.8"/>',
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{color}"/>',
        "</svg>",
    ]
    return "".join(parts)
