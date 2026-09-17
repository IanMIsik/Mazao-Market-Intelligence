"""
SVG geometry for the Live Market Generation tab. Own module, not folded
into charts_live.py or charts_bess.py: the category-bars chart's x-axis
is 4 fixed categories, not the settlement-period-indexed multiples
charts_live.py's _axes()/_sp_ticks() are built for, and the donuts here
are keyed by fuel type/emissions source, not participant.

Both donuts (Generation Mix and Carbon Intensity) share one renderer,
`donut_svg()`, which reuses charts_bess.py's stacked-<circle>-stroke
technique directly (same trick: stroke-dasharray splits the circle's
outline into one visible arc plus one transparent remainder,
stroke-dashoffset shifts where that arc starts) and the page's existing
.donut-* CSS classes, so all the donuts on this site look and behave the
same way. Each caller resolves its own labels/colors/tooltip text first
-- `donut_svg()` itself has no fuel-type knowledge. `segment_color()`/
`segment_label()` are a second shared pair, this time between
carbon_intensity_donut_svg() and generation_category_bars_svg() -- both
work over the same small set of segment "kind"s (fuel/solar/import,
plus emissions_mix()'s own "other_import").
"""

from __future__ import annotations

import math
from html import escape

# Display name and stroke color per FUELINST fuel type. Colors are CSS
# variables (--lm-fuel-*, see app.css) rather than the BESS donut's
# Python-side hex palette -- there are only ~10 fuel types, a small fixed
# set like service_type's PALETTE in charts_bess.py, so each gets a
# deliberately chosen, stable color instead of one assigned by list
# position (which would shuffle colors around as fuel types come and go
# from a given snapshot).
FUEL_LABELS: dict[str, str] = {
    "CCGT": "Gas (CCGT)",
    "OCGT": "Gas (OCGT)",
    "NUCLEAR": "Nuclear",
    "WIND": "Wind",
    "BIOMASS": "Biomass",
    "COAL": "Coal",
    "NPSHYD": "Hydro",
    "OIL": "Oil",
    "OTHER": "Other",
    "PS": "Pumped storage",
}

FUEL_COLOR_VARS: dict[str, str] = {
    "CCGT": "var(--lm-fuel-ccgt)",
    "OCGT": "var(--lm-fuel-ocgt)",
    "NUCLEAR": "var(--lm-fuel-nuclear)",
    "WIND": "var(--lm-teal)",
    "BIOMASS": "var(--lm-fuel-biomass)",
    "COAL": "var(--lm-fuel-coal)",
    "NPSHYD": "var(--lm-fuel-npshyd)",
    "OIL": "var(--lm-fuel-oil)",
    "OTHER": "var(--lm-fuel-other)",
    "PS": "var(--lm-fuel-ps)",
}
FALLBACK_COLOR = "var(--lm-faint)"

DONUT_LABEL_MIN_FRACTION = 0.04  # same threshold as charts_bess.py's donut -- smaller slices get a tooltip only


def fuel_label(fuel_type: str) -> str:
    return FUEL_LABELS.get(fuel_type, fuel_type)


def fuel_color(fuel_type: str) -> str:
    return FUEL_COLOR_VARS.get(fuel_type, FALLBACK_COLOR)


DEFAULT_DONUT_STROKE_WIDTH = 46  # Generation Mix / Carbon Intensity's own thickness -- unchanged
PF_DONUT_STROKE_WIDTH = 10  # GB Power Flow's two donuts (BM Generation, sources) -- deliberately thin, by request


def donut_svg(
    element_id: str, aria_label: str, slices: list[dict],
    stroke_width: int = DEFAULT_DONUT_STROKE_WIDTH, show_labels: bool = True,
) -> str:
    """Shared donut renderer -- `slices` is [{"label", "color", "value"
    (arc-sizing weight, e.g. MW or a %), "title" (full tooltip text)}],
    already resolved by the caller. See generation_mix_donut_svg()/
    carbon_intensity_donut_svg() below for the two current callers.
    `stroke_width`/`show_labels` default to the original thick-ring,
    percentage-labeled look; GB Power Flow's two donuts pass a thinner
    width and turn labels off (by request -- see power_flow_metrics.py's
    callers) since they're small, secondary elements in a bigger diagram
    where a thick ring and text would overwhelm everything else on it.
    `radius` -- and so the ring's own outer diameter -- stays constant
    regardless of stroke width, so thinning the ring only opens up more
    room in the hole for the centered label, not shrink the whole donut.
    """
    if not slices:
        return ""
    total = sum(r["value"] for r in slices) or 1.0

    radius = 72
    cx = cy = radius + stroke_width / 2 + 6
    circumference = 2 * math.pi * radius
    size = cx * 2

    parts = [
        f'<svg id="{element_id}" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="{escape(aria_label)}" preserveAspectRatio="xMidYMid meet">',
        f'<g transform="rotate(-90 {cx} {cy})">',
    ]
    labels: list[str] = []

    cumulative = 0.0
    for r in slices:
        frac = r["value"] / total
        arc = frac * circumference
        label = escape(r["label"])
        parts.append(
            f'<circle class="donut-slice" cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
            f'stroke="{r["color"]}" stroke-width="{stroke_width}" '
            f'stroke-dasharray="{arc:.2f} {circumference - arc:.2f}" stroke-dashoffset="{-cumulative:.2f}" '
            f'data-label="{label}" data-value="{r["value"]:.1f}" data-pct="{frac * 100:.1f}">'
            f'<title>{escape(r["title"])}</title>'
            f"</circle>"
        )
        if show_labels and frac >= DONUT_LABEL_MIN_FRACTION:
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


def generation_mix_donut_svg(
    by_fuel: list[dict], element_id: str = "generation-mix-donut",
    stroke_width: int = DEFAULT_DONUT_STROKE_WIDTH, show_labels: bool = True,
) -> str:
    """Donut chart, one segment per fuel type, sized by share of current
    total generation. Callers filter out non-positive readings first (a
    fuel type can be momentarily negative -- pumped storage while
    charging -- and a donut can't represent a negative share of a whole),
    same responsibility split as charts_bess.py's daily_revenue_donut_svg().

    `element_id` defaults to the Generation Mix card's own id; GB Power
    Flow's BM Generation ring reuses this same renderer for its own
    fuel-type breakdown (see power_flow_metrics.py's `bm_mix`) with a
    different id, since two donuts can't share one DOM id on the same
    page (the click-to-select script looks elements up by id), and a
    thinner `stroke_width`/`show_labels=False` to match that card's own
    thin, unlabeled donut style.
    """
    total = sum(r["generation_mw"] for r in by_fuel) or 1.0
    slices = [
        {
            "label": fuel_label(r["fuel_type"]),
            "color": fuel_color(r["fuel_type"]),
            "value": r["generation_mw"],
            "title": f"{fuel_label(r['fuel_type'])}: {r['generation_mw']:,.0f} MW ({r['generation_mw'] / total * 100:.1f}%)",
        }
        for r in by_fuel
    ]
    return donut_svg(element_id, "Current generation mix by fuel type", slices, stroke_width=stroke_width, show_labels=show_labels)


# GB Power Flow's own ring colors (see app.css's .pf-ring-* rules,
# hex values read from the reference dashboard's compiled CSS) -- reused
# here so the central "sources" donut's slices match the ring each
# slice summarizes.
PF_SOURCE_COLORS: dict[str, str] = {"bm": "#3991c0", "import": "#a97ab0", "wind": "#1eb097", "solar": "#ee854a"}
PF_SOURCE_LABELS: dict[str, str] = {
    "bm": "BM Generation", "import": "Imports", "wind": "LV Wind", "solar": "Embedded Solar",
}


def power_flow_sources_donut_svg(flow: dict) -> str:
    """Donut chart for GB Power Flow's center -- one segment per
    generation source feeding in (BM Generation, Imports, LV Wind,
    Embedded Solar), sized by GW. A source with no reading yet today is
    simply left out, not shown as a fabricated zero slice. Thin ring,
    no percentage labels -- same PF_DONUT_STROKE_WIDTH treatment as the
    BM Generation donut on this same card, by request.
    """
    sources = [
        ("bm", flow.get("bm_generation_mw")),
        ("import", flow.get("imports_mw")),
        ("wind", flow.get("embedded_wind_mw")),
        ("solar", flow.get("embedded_solar_mw")),
    ]
    slices = [
        {
            "label": PF_SOURCE_LABELS[key],
            "color": PF_SOURCE_COLORS[key],
            "value": mw,
            "title": f"{PF_SOURCE_LABELS[key]}: {mw / 1000:.2f} GW",
        }
        for key, mw in sources
        if mw is not None and mw > 0
    ]
    return donut_svg(
        "pf-sources-donut", "GB generation by source", slices,
        stroke_width=PF_DONUT_STROKE_WIDTH, show_labels=False,
    )


IMPORT_SEGMENT_COLOR = "var(--lm-violet)"  # matches the Imports/Exports rings on the GB Power Flow card
SOLAR_SEGMENT_COLOR = "var(--lm-gold)"  # matches the Fundamentals tab's own solar color


def segment_color(seg: dict) -> str:
    """Shared by carbon_intensity_donut_svg() and
    generation_category_bars_svg() -- both work over the same small set
    of segment "kind"s (fuel/solar/import, plus emissions_mix()'s own
    "other_import"), so one resolver covers both."""
    if seg["kind"] == "fuel":
        return fuel_color(seg["key"])
    if seg["kind"] == "solar":
        return SOLAR_SEGMENT_COLOR
    return IMPORT_SEGMENT_COLOR  # import or other_import


def segment_label(seg: dict) -> str:
    if seg["kind"] == "fuel":
        return fuel_label(seg["key"])
    if seg["kind"] == "solar":
        return "Solar"
    return seg["label"] or seg["key"]


def carbon_intensity_donut_svg(segments: list[dict]) -> str:
    """Donut chart, one segment per emissions contributor -- see
    carbon_intensity_metrics.emissions_mix() -- sized by *share of
    emissions* (generation x its official gCO2/kWh factor), not share of
    generation, so e.g. a small coal/gas slice can still dominate the
    donut. The hover title spells out the factor and generation amount
    behind each slice's %, per the reference's own "hover to see the
    different CIs and how these are calculated".
    """
    slices = [
        {
            "label": segment_label(s),
            "color": segment_color(s),
            "value": s["emissions"],
            "title": (
                f"{segment_label(s)}: {s['pct']:.1f}% of emissions -- "
                f"{s['factor']:.0f} gCO2/kWh x {s['generation_mw'] / 1000:.2f} GW"
            ),
        }
        for s in segments
    ]
    return donut_svg("carbon-intensity-donut", "Carbon intensity by source, weighted by emissions", slices)


def _nice_axis(max_val: float, target_ticks: int = 5) -> tuple[float, float]:
    """Same 1/2/2.5/5 x 10^n 'nice number' axis-rounding as
    charts_bess.py's _nice_axis()."""
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


BAR_AXIS_LEFT = 46.0
BAR_PLOT_W = 320.0
BAR_PLOT_TOP = 16.0
BAR_PLOT_H = 420.0  # tall viewBox on purpose -- once scaled to the card's own narrow width (see
# app.css's .lm-gen-card), the original 382:276 (roughly landscape) viewBox left most of the
# card's height empty below the chart; 382:476 fills that space without the bars becoming
# absurdly narrow/tall at the card's actual rendered width (~288px)
BAR_GAP = 22.0
BAR_VIEW_W = BAR_AXIS_LEFT + BAR_PLOT_W + 16
BAR_VIEW_H = BAR_PLOT_TOP + BAR_PLOT_H + 40
SEGMENT_LABEL_MIN_HEIGHT = 13.0  # px -- below this a % label wouldn't fit; tooltip-only, same idea as the donut's own DONUT_LABEL_MIN_FRACTION


def generation_category_bars_svg(categories: list[dict]) -> str:
    """4 fixed bars (Fossil Fuels / Renewables / Low Carbon / Other),
    each subdivided into its actual contributors -- see
    fuelinst_metrics.current_mix_by_category()'s `segments` -- with each
    segment's own GW amount labeled on any slice tall enough to hold one
    (a full MW + % breakdown is always in the hover title either way),
    and that category's share of *all four bars combined* labeled above
    the bar. Bar height (and that share) is based on a category's
    *drawn* segments, not its raw net total -- those only ever differ
    when a category's total is quietly reduced by a negative contributor
    that can't itself be drawn as a slice (e.g. Other during heavy
    pumped-storage charging), and keeping the bar's own height the
    source of truth avoids a number that doesn't agree with what's on
    screen.
    """
    if not categories:
        return ""
    bars = [
        {"category": c["category"], "segments": [s for s in c["segments"] if s["generation_mw"] > 0]}
        for c in categories
    ]
    bar_totals = [sum(s["generation_mw"] for s in b["segments"]) for b in bars]
    grand_total = sum(bar_totals) or 1.0
    axis_max, step = _nice_axis(max(bar_totals + [0.0]))
    v_span = axis_max or 1.0
    zero_y = BAR_PLOT_TOP + BAR_PLOT_H

    def y(v: float) -> float:
        return zero_y - (v / v_span) * BAR_PLOT_H

    n = len(bars)
    bar_w = (BAR_PLOT_W - BAR_GAP * (n - 1)) / n

    parts = [
        f'<svg viewBox="0 0 {BAR_VIEW_W:.0f} {BAR_VIEW_H:.0f}" role="img" '
        f'aria-label="Current generation by category, subdivided by source" preserveAspectRatio="xMidYMid meet">',
        f'<text class="ax sm unit" x="{BAR_AXIS_LEFT - 6:.1f}" y="{BAR_PLOT_TOP - 6:.1f}" text-anchor="end">MW</text>',
    ]

    n_ticks = int(round(axis_max / step)) if step else 0
    for k in range(0, n_ticks + 1):
        t = step * k
        gy = y(t)
        parts.append(f'<line class="grid" x1="{BAR_AXIS_LEFT}" x2="{BAR_AXIS_LEFT + BAR_PLOT_W}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="ax sm" x="{BAR_AXIS_LEFT - 6:.1f}" y="{gy + 3.5:.1f}" text-anchor="end">{t:,.0f}</text>')

    for i, b in enumerate(bars):
        category, segments, bar_total = b["category"], b["segments"], bar_totals[i]

        cumulative = 0.0
        bx = BAR_AXIS_LEFT + i * (bar_w + BAR_GAP)
        for seg in segments:
            y_bottom, y_top = y(cumulative), y(cumulative + seg["generation_mw"])
            h = y_bottom - y_top
            pct = seg["generation_mw"] / (bar_total or 1.0) * 100
            label = escape(segment_label(seg))
            parts.append(
                f'<rect x="{bx:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{segment_color(seg)}" '
                f'stroke="var(--lm-bg)" stroke-width="1">'
                f'<title>{label}: {seg["generation_mw"]:,.0f} MW ({pct:.1f}%)</title></rect>'
            )
            if h >= SEGMENT_LABEL_MIN_HEIGHT:
                parts.append(
                    f'<text class="donut-pct" x="{bx + bar_w / 2:.1f}" y="{(y_top + y_bottom) / 2:.1f}" '
                    f'text-anchor="middle" dominant-baseline="central">{seg["generation_mw"] / 1000:.1f}GW</text>'
                )
            cumulative += seg["generation_mw"]

        top_y = y(bar_total) if segments else zero_y
        share_pct = bar_total / grand_total * 100
        parts.append(
            f'<text class="ax sm" x="{bx + bar_w / 2:.1f}" y="{top_y - 6:.1f}" text-anchor="middle">{share_pct:.0f}%</text>'
        )
        parts.append(
            f'<text class="ax sm" x="{bx + bar_w / 2:.1f}" y="{BAR_PLOT_TOP + BAR_PLOT_H + 14:.1f}" '
            f'text-anchor="middle">{escape(category)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
