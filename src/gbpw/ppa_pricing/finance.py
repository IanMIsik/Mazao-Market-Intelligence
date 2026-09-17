"""
Pure time-value-of-money math -- NPV and IRR. No project/PPA knowledge
here at all (see cashflow.py for that); this module only knows about a
plain list of annual cashflows, index 0 = year 0 (undiscounted).
"""

from __future__ import annotations

IRR_LOW_BOUND = -0.99  # rate must stay > -100% (1+rate must be positive)
IRR_HIGH_BOUND = 10.0  # 1000% -- generously wide; a real project's IRR is nowhere near this
IRR_TOL = 1e-9
IRR_MAX_ITER = 200


def npv(rate: float, cashflows: list[float]) -> float:
    """cashflows[0] is undiscounted (year 0); cashflows[t] is discounted
    by (1+rate)^t."""
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))


def _count_sign_changes(cashflows: list[float]) -> int:
    signs = [1 if cf > 0 else -1 for cf in cashflows if cf != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def irr(
    cashflows: list[float],
    low: float = IRR_LOW_BOUND,
    high: float = IRR_HIGH_BOUND,
    tol: float = IRR_TOL,
    max_iter: int = IRR_MAX_ITER,
) -> float:
    """The discount rate at which npv(rate, cashflows) == 0, via bisection
    on NPV(rate). Only well-defined (a unique root -- Descartes' rule) for
    a cashflow with exactly one sign change: a negative outflow at t=0
    followed eventually by positive net cashflows, never flipping sign
    more than once. That's the normal shape for a generation project
    (CAPEX outflow, then revenue-cost inflows every year) -- anything
    else raises rather than silently returning one root of several or a
    meaningless number. NPV(rate) is strictly decreasing in rate for this
    cashflow shape, so bisection (no derivative needed, guaranteed
    convergence given a bracketing interval) is simple and robust; see
    ppa_pricing/solver.py's own docstring for the fuller reasoning
    against Newton-Raphson/Brent for this project's actual use.
    """
    if not cashflows or cashflows[0] >= 0:
        raise ValueError("irr() requires a negative cashflow at year 0 (an initial investment)")
    sign_changes = _count_sign_changes(cashflows)
    if sign_changes != 1:
        raise ValueError(
            f"irr() is only well-defined for a cashflow with exactly one sign change "
            f"(one outflow, then sustained positive returns) -- got {sign_changes}"
        )

    npv_low = npv(low, cashflows)
    npv_high = npv(high, cashflows)
    if npv_low * npv_high > 0:
        raise ValueError(
            f"No root bracketed in [{low:.2%}, {high:.2%}] -- NPV has the same sign at both ends "
            f"(NPV({low:.2%})={npv_low:,.2f}, NPV({high:.2%})={npv_high:,.2f})"
        )

    mid = (low + high) / 2
    for _ in range(max_iter):
        mid = (low + high) / 2
        npv_mid = npv(mid, cashflows)
        if abs(npv_mid) < tol or (high - low) < tol:
            break
        if npv_low * npv_mid < 0:
            high = mid
        else:
            low, npv_low = mid, npv_mid
    return mid
