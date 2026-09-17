"""
Project generation, revenue, costs, debt and annual cashflow assembly for
the PPA Pricing Engine. Pure functions over ProjectAssumptions -- no
database, no HTTP. Every assumption is an explicit named field here, not
a magic constant buried in a formula (direct requirement from the
brief this engine was scoped from).

Only a Fixed-price PPA structure is implemented (revenue = generation x
a single flat £/MWh, no CPI/market-linked escalation) -- see
annual_ppa_revenue()'s docstring for how later PPA structures would slot
in without changing anything else in this module.

Units, stated once: capacity_mw (MW), capacity_factor/availability/
curtailment_pct/degradation_pct/inflation_pct/interest_rate/gearing_pct
(0-1 fractions, not percentages), capex_per_mw/opex_per_mw_year
(£/MW and £/MW/year respectively), generation (MWh), every money figure
in the output cashflows is £ (not £m, not £/MWh).
"""

from __future__ import annotations

from dataclasses import dataclass

HOURS_PER_YEAR = 8760.0


@dataclass(frozen=True)
class ProjectAssumptions:
    capacity_mw: float
    capacity_factor: float  # 0-1, year-1 (pre-degradation)
    availability: float  # 0-1
    curtailment_pct: float  # 0-1, fraction of potential output lost to curtailment
    degradation_pct: float  # 0-1, annual output decline
    capex_per_mw: float  # GBP/MW, one-off at year 0
    opex_per_mw_year: float  # GBP/MW/year, year-1 (pre-inflation)
    inflation_pct: float  # 0-1, annual OPEX escalation
    ppa_term_years: int
    gearing_pct: float  # 0-1, debt as a fraction of CAPEX
    interest_rate: float  # 0-1, annual, on the debt balance
    debt_term_years: int  # <= ppa_term_years; 0 or gearing_pct=0 means no debt


def capex_total(a: ProjectAssumptions) -> float:
    return a.capex_per_mw * a.capacity_mw


def annual_generation_mwh(a: ProjectAssumptions) -> list[float]:
    """Flat capacity-factor model -- not a half-hourly shape. Appropriate
    here specifically because this project doesn't exist yet and so has
    no real dispatch history to build a genuine capture-price join from
    (see ppa_pricing/__init__.py's module docstring); ppa_metrics.py's
    Sigma(generation x price)/Sigma(generation) engine is for analyzing
    an asset that already has one.
    """
    base = a.capacity_mw * a.capacity_factor * HOURS_PER_YEAR * a.availability * (1 - a.curtailment_pct)
    return [base * (1 - a.degradation_pct) ** year for year in range(a.ppa_term_years)]


def annual_opex(a: ProjectAssumptions) -> list[float]:
    base = a.opex_per_mw_year * a.capacity_mw
    return [base * (1 + a.inflation_pct) ** year for year in range(a.ppa_term_years)]


def annual_ppa_revenue(generation_mwh: list[float], ppa_price: float) -> list[float]:
    """Fixed-price PPA: revenue_t = generation_t * ppa_price, flat
    nominal £/MWh across the whole term. A later CPI-linked/market-linked/
    floor-collar structure would replace this one line with a per-year
    effective price function of (year, ppa_price, ...); nothing else in
    this module or solver.py depends on the structure being fixed.
    """
    return [g * ppa_price for g in generation_mwh]


@dataclass(frozen=True)
class DebtYear:
    year: int  # 1-based
    opening_balance: float
    interest: float
    principal: float
    payment: float
    closing_balance: float


def debt_schedule(a: ProjectAssumptions) -> list[DebtYear]:
    """Equal-installment amortizing loan over debt_term_years at
    interest_rate on the declining balance -- a deliberate simplification
    versus real DSCR-sculpted project debt (explicitly future/V2 scope,
    disclosed on the page's own methodology note, not hidden).
    """
    principal_total = capex_total(a) * a.gearing_pct
    if principal_total <= 0 or a.debt_term_years <= 0:
        return []

    r, n = a.interest_rate, a.debt_term_years
    payment = principal_total / n if r == 0 else principal_total * r / (1 - (1 + r) ** -n)

    schedule = []
    balance = principal_total
    for year in range(1, n + 1):
        interest = balance * r
        principal_payment = payment - interest
        closing = balance - principal_payment
        schedule.append(DebtYear(year, balance, interest, principal_payment, payment, closing))
        balance = closing
    return schedule


def annual_debt_service(a: ProjectAssumptions) -> list[float]:
    """Debt service per PPA-term year -- 0 once the loan is repaid (when
    debt_term_years < ppa_term_years), never negative/extended."""
    schedule = debt_schedule(a)
    service = [d.payment for d in schedule]
    service += [0.0] * (a.ppa_term_years - len(service))
    return service[: a.ppa_term_years]


@dataclass(frozen=True)
class CashflowResult:
    generation_mwh: list[float]
    revenue: list[float]
    opex: list[float]
    ebitda: list[float]
    debt_service: list[float]
    project_cashflow: list[float]  # [0] = -CAPEX, [1:] = EBITDA per year (unlevered)
    equity_cashflow: list[float]  # [0] = -(CAPEX*(1-gearing)), [1:] = EBITDA - debt service


def build_cashflows(a: ProjectAssumptions, ppa_price: float) -> CashflowResult:
    generation = annual_generation_mwh(a)
    revenue = annual_ppa_revenue(generation, ppa_price)
    opex = annual_opex(a)
    ebitda = [r - o for r, o in zip(revenue, opex)]
    debt_service = annual_debt_service(a)

    capex = capex_total(a)
    equity_capex = capex * (1 - a.gearing_pct)
    project_cf = [-capex, *ebitda]
    equity_cf = [-equity_capex, *(e - d for e, d in zip(ebitda, debt_service))]

    return CashflowResult(generation, revenue, opex, ebitda, debt_service, project_cf, equity_cf)
