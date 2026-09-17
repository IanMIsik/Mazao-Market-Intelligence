import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ppa_pricing.cashflow import (  # noqa: E402
    ProjectAssumptions,
    annual_debt_service,
    annual_generation_mwh,
    annual_opex,
    annual_ppa_revenue,
    build_cashflows,
    capex_total,
    debt_schedule,
)


def _assumptions(**overrides):
    base = dict(
        capacity_mw=10.0, capacity_factor=0.25, availability=1.0, curtailment_pct=0.0,
        degradation_pct=0.0, capex_per_mw=800_000.0, opex_per_mw_year=0.0, inflation_pct=0.0,
        ppa_term_years=15, gearing_pct=0.0, interest_rate=0.0, debt_term_years=0,
    )
    base.update(overrides)
    return ProjectAssumptions(**base)


def test_generation_flat_when_no_degradation():
    gen = annual_generation_mwh(_assumptions())
    expected = 10.0 * 0.25 * 8760  # capacity_mw * capacity_factor * hours/year
    assert gen == pytest.approx([expected] * 15)


def test_generation_applies_availability_and_curtailment():
    gen = annual_generation_mwh(_assumptions(availability=0.98, curtailment_pct=0.02))
    expected = 10.0 * 0.25 * 8760 * 0.98 * 0.98
    assert gen[0] == pytest.approx(expected)


def test_generation_degrades_each_year():
    gen = annual_generation_mwh(_assumptions(degradation_pct=0.005, ppa_term_years=3))
    base = 10.0 * 0.25 * 8760
    assert gen == pytest.approx([base, base * 0.995, base * 0.995**2])


def test_opex_escalates_with_inflation():
    opex = annual_opex(_assumptions(opex_per_mw_year=1000.0, inflation_pct=0.02, ppa_term_years=3))
    assert opex == pytest.approx([10_000.0, 10_200.0, 10_404.0])


def test_ppa_revenue_is_generation_times_flat_price():
    assert annual_ppa_revenue([100.0, 90.0], 50.0) == pytest.approx([5000.0, 4500.0])


def test_capex_total_scales_with_capacity():
    assert capex_total(_assumptions(capacity_mw=5.0, capex_per_mw=800_000.0)) == pytest.approx(4_000_000.0)


def test_debt_schedule_empty_when_no_gearing():
    assert debt_schedule(_assumptions(gearing_pct=0.0, debt_term_years=10)) == []


def test_debt_schedule_zero_interest_is_straight_line():
    a = _assumptions(capacity_mw=1.0, capex_per_mw=1000.0, gearing_pct=1.0, interest_rate=0.0, debt_term_years=5)
    schedule = debt_schedule(a)
    assert [round(d.payment, 2) for d in schedule] == [200.0] * 5
    assert schedule[-1].closing_balance == pytest.approx(0.0, abs=1e-6)


def test_debt_schedule_amortizes_fully_by_final_year():
    a = _assumptions(capacity_mw=1.0, capex_per_mw=1000.0, gearing_pct=1.0, interest_rate=0.05, debt_term_years=5)
    schedule = debt_schedule(a)
    assert schedule[-1].closing_balance == pytest.approx(0.0, abs=1e-4)
    # every payment is the same size (equal-installment amortization)
    payments = {round(d.payment, 4) for d in schedule}
    assert len(payments) == 1
    # total principal repaid across the schedule equals the original loan
    assert sum(d.principal for d in schedule) == pytest.approx(1000.0, abs=1e-4)


def test_debt_service_zero_after_debt_is_repaid():
    a = _assumptions(capacity_mw=1.0, capex_per_mw=1000.0, gearing_pct=1.0, interest_rate=0.05,
                      debt_term_years=3, ppa_term_years=5)
    service = annual_debt_service(a)
    assert len(service) == 5
    assert all(s > 0 for s in service[:3])
    assert service[3:] == [0.0, 0.0]


def test_build_cashflows_project_vs_equity_outflow():
    a = _assumptions(capacity_mw=10.0, capex_per_mw=800_000.0, gearing_pct=0.7, interest_rate=0.06,
                      debt_term_years=15, opex_per_mw_year=5000.0)
    cf = build_cashflows(a, ppa_price=60.0)

    capex = capex_total(a)
    assert cf.project_cashflow[0] == pytest.approx(-capex)
    assert cf.equity_cashflow[0] == pytest.approx(-capex * 0.3)
    assert cf.ebitda[0] == pytest.approx(cf.revenue[0] - cf.opex[0])
    assert cf.project_cashflow[1] == pytest.approx(cf.ebitda[0])
    assert cf.equity_cashflow[1] == pytest.approx(cf.ebitda[0] - cf.debt_service[0])
