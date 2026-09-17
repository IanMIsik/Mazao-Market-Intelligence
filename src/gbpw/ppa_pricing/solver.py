"""
Solves backward from a target IRR to the required fixed PPA price.

Why bisection, not Newton-Raphson or Brent: for a Fixed PPA, revenue (and
so IRR) is strictly increasing in ppa_price -- more £/MWh on fixed
generation can only increase revenue. That monotonicity is exactly what
bisection needs (no derivative, guaranteed convergence given a bracket),
and it makes infeasibility trivial to detect at the two bracket ends
before ever iterating. Newton-Raphson would need a derivative that has
no closed form once degradation/inflation/debt amortization are stacked,
and can diverge outside a good starting guess; Brent's method is a
reasonable alternative but adds complexity this single well-behaved
monotonic root doesn't need. finance.irr() itself uses the same
reasoning one level down (bisection on NPV(rate)=0).
"""

from __future__ import annotations

from .cashflow import ProjectAssumptions, build_cashflows
from .finance import irr as compute_irr
from .finance import npv as compute_npv

PRICE_TOL = 1e-4  # GBP/MWh
IRR_MATCH_TOL = 1e-7
MAX_ITER = 100


class InfeasiblePrice(Exception):
    """No PPA price within price_bounds achieves the requested target IRR."""


def _irr_or_neg_inf(cashflow: list[float]) -> float:
    """A project earning zero/negative EBITDA every year (e.g. ppa_price=0
    with real OPEX) has no well-defined IRR at all -- finance.irr() raises
    on a cashflow with no sign change. For bisection purposes that's
    "worse than any achievable target", not an error to propagate; -inf
    compares correctly against any realistic target_irr either way.
    """
    try:
        return compute_irr(cashflow)
    except ValueError:
        return float("-inf")


def solve_ppa_price(
    assumptions: ProjectAssumptions,
    target_irr: float,
    target: str = "project",
    price_bounds: tuple[float, float] = (0.0, 500.0),
    discount_rate: float | None = None,
) -> dict:
    """target is "project" (unlevered IRR, ignores financing) or "equity"
    (levered IRR, net of debt service). Returns the required price plus
    a full breakdown of the cashflow it produces -- this *is* the
    "explain the price" feature: every figure below is a real
    intermediate value from the same calculation, not a separate model.
    Raises InfeasiblePrice (not a wrong number) if target_irr is already
    met at the bottom of price_bounds, or unreachable at the top.
    """
    if target not in ("project", "equity"):
        raise ValueError(f"target must be 'project' or 'equity', got {target!r}")

    def irr_at(price: float) -> float:
        cf = build_cashflows(assumptions, price)
        cashflow = cf.project_cashflow if target == "project" else cf.equity_cashflow
        return _irr_or_neg_inf(cashflow)

    lo, hi = price_bounds
    irr_lo, irr_hi = irr_at(lo), irr_at(hi)

    if irr_lo >= target_irr:
        raise InfeasiblePrice(
            f"Target {target} IRR of {target_irr:.1%} is already met at the lowest price in range "
            f"(£{lo:.2f}/MWh gives {irr_lo:.1%}) -- this project doesn't need a PPA to hit that return."
        )
    if irr_hi < target_irr:
        raise InfeasiblePrice(
            f"Target {target} IRR of {target_irr:.1%} is not achievable even at the highest price in "
            f"range (£{hi:.2f}/MWh only gives {irr_hi:.1%})."
        )

    mid = (lo + hi) / 2
    for _ in range(MAX_ITER):
        mid = (lo + hi) / 2
        irr_mid = irr_at(mid)
        if abs(irr_mid - target_irr) < IRR_MATCH_TOL or (hi - lo) < PRICE_TOL:
            break
        if irr_mid < target_irr:
            lo = mid
        else:
            hi = mid

    price = mid
    cf = build_cashflows(assumptions, price)
    cashflow = cf.project_cashflow if target == "project" else cf.equity_cashflow
    achieved_irr = compute_irr(cashflow)
    npv_at_discount = compute_npv(discount_rate, cashflow) if discount_rate is not None else None

    return {
        "ppa_price_gbp_mwh": round(price, 2),
        "target": target,
        "target_irr": target_irr,
        "achieved_irr": achieved_irr,
        "discount_rate": discount_rate,
        "npv_at_discount_rate_gbp": round(npv_at_discount, 0) if npv_at_discount is not None else None,
        "generation_mwh_year1": round(cf.generation_mwh[0], 1),
        "total_generation_mwh": round(sum(cf.generation_mwh), 1),
        "revenue_year1_gbp": round(cf.revenue[0], 0),
        "opex_year1_gbp": round(cf.opex[0], 0),
        "ebitda_year1_gbp": round(cf.ebitda[0], 0),
        "capex_gbp": round(assumptions.capex_per_mw * assumptions.capacity_mw, 0),
        "debt_service_year1_gbp": round(cf.debt_service[0], 0) if cf.debt_service and cf.debt_service[0] else 0.0,
        "project_cashflow": [round(c, 0) for c in cf.project_cashflow],
        "equity_cashflow": [round(c, 0) for c in cf.equity_cashflow],
    }
