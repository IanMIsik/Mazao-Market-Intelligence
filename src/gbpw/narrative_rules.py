"""
Deterministic, threshold-free narrative fallback.

No external calls, no randomness, always succeeds -- this is what the page
falls back to when narrative_llm.generate() returns None. Same output shape:
{"headline", "byline", "drivers": [3 strings]}. Every word comes directly
from metrics.build_week()'s facts dict; nothing here is invented.
"""

from __future__ import annotations


def generate(facts: dict) -> dict:
    kpi = facts["kpi"]
    drivers = facts["drivers"]

    change = kpi["avg_day_ahead_pct_change"]
    if change is None:
        lede = "Average day-ahead priced at"
    elif change >= 0:
        lede = f"Average day-ahead rose {change}% on the prior week, to"
    else:
        lede = f"Average day-ahead fell {abs(change)}% on the prior week, to"

    headline = f"{lede} £{kpi['avg_day_ahead']}/MWh, week ending {facts['week_ending']}."

    byline = (
        f"Best 1-hour battery spread of the week was £{kpi['best_spread_week']}/MWh, "
        f"on {kpi['best_spread_week_day']}. Wind share of generation was {kpi['wind_share_pct']}%."
    )

    wind_driver = (
        f"Wind output ranged from {drivers['wind']['low_gw']} GW on {drivers['wind']['low_day']} "
        f"to {drivers['wind']['high_gw']} GW on {drivers['wind']['high_day']}."
    )
    demand_driver = f"Peak demand for the week was {drivers['demand']['peak_gw']} GW, on {drivers['demand']['peak_day']}."
    p100_driver = (
        f"{drivers['periods_above_100']['high']} settlement periods cleared above £100 on "
        f"{drivers['periods_above_100']['high_day']}, against {drivers['periods_above_100']['low']} "
        f"on {drivers['periods_above_100']['low_day']}."
    )

    return {"headline": headline, "byline": byline, "drivers": [wind_driver, demand_driver, p100_driver]}
