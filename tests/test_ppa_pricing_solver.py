import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ppa_pricing.cashflow import ProjectAssumptions, annual_generation_mwh, capex_total, debt_schedule  # noqa: E402
from gbpw.ppa_pricing.solver import InfeasiblePrice, solve_ppa_price  # noqa: E402


def _assumptions(**overrides):
    base = dict(
        capacity_mw=10.0, capacity_factor=0.25, availability=1.0, curtailment_pct=0.0,
        degradation_pct=0.0, capex_per_mw=800_000.0, opex_per_mw_year=0.0, inflation_pct=0.0,
        ppa_term_years=15, gearing_pct=0.0, interest_rate=0.0, debt_term_years=0,
    )
    base.update(overrides)
    return ProjectAssumptions(**base)


def test_zero_target_irr_no_opex_no_debt_is_simple_breakeven():
    # No time value of money, no costs: price * total generation = capex, exactly.
    a = _assumptions()
    result = solve_ppa_price(a, target_irr=0.0, target="project")

    gen = annual_generation_mwh(a)
    expected_price = capex_total(a) / sum(gen)
    assert result["ppa_price_gbp_mwh"] == pytest.approx(expected_price, abs=0.01)
    assert result["achieved_irr"] == pytest.approx(0.0, abs=1e-5)


def test_positive_target_irr_no_opex_no_debt_matches_annuity_formula():
    a = _assumptions()
    target_irr = 0.08
    result = solve_ppa_price(a, target_irr=target_irr, target="project")

    gen = annual_generation_mwh(a)
    # capex = sum(gen_t * price / (1+irr)^t)  =>  price = capex / sum(gen_t / (1+irr)^t)
    discounted_gen = sum(g / (1 + target_irr) ** (t + 1) for t, g in enumerate(gen))
    expected_price = capex_total(a) / discounted_gen
    assert result["ppa_price_gbp_mwh"] == pytest.approx(expected_price, abs=0.01)
    assert result["achieved_irr"] == pytest.approx(target_irr, abs=1e-5)


def test_degradation_reduces_discounted_generation_and_raises_required_price():
    a_flat = _assumptions()
    a_degraded = _assumptions(degradation_pct=0.005)
    target_irr = 0.08

    price_flat = solve_ppa_price(a_flat, target_irr, target="project")["ppa_price_gbp_mwh"]
    price_degraded = solve_ppa_price(a_degraded, target_irr, target="project")["ppa_price_gbp_mwh"]

    gen = annual_generation_mwh(a_degraded)
    discounted_gen = sum(g / (1 + target_irr) ** (t + 1) for t, g in enumerate(gen))
    expected_price = capex_total(a_degraded) / discounted_gen
    assert price_degraded == pytest.approx(expected_price, abs=0.01)
    # less lifetime generation to sell at the same target return -> higher required price
    assert price_degraded > price_flat


def test_opex_increases_required_price_by_its_discounted_value():
    a = _assumptions(opex_per_mw_year=15_000.0, inflation_pct=0.02)
    target_irr = 0.08
    result = solve_ppa_price(a, target_irr=target_irr, target="project")

    from gbpw.ppa_pricing.cashflow import annual_opex
    gen = annual_generation_mwh(a)
    opex = annual_opex(a)
    discounted_gen = sum(g / (1 + target_irr) ** (t + 1) for t, g in enumerate(gen))
    discounted_opex = sum(o / (1 + target_irr) ** (t + 1) for t, o in enumerate(opex))
    expected_price = (capex_total(a) + discounted_opex) / discounted_gen
    assert result["ppa_price_gbp_mwh"] == pytest.approx(expected_price, abs=0.01)


def test_equity_irr_target_with_debt_matches_hand_formula():
    a = _assumptions(opex_per_mw_year=10_000.0, gearing_pct=0.7, interest_rate=0.06, debt_term_years=15)
    target_irr = 0.10
    result = solve_ppa_price(a, target_irr=target_irr, target="equity")

    from gbpw.ppa_pricing.cashflow import annual_opex
    gen = annual_generation_mwh(a)
    opex = annual_opex(a)
    debt_payment = debt_schedule(a)[0].payment  # equal-installment -> same every year
    equity_capex = capex_total(a) * (1 - a.gearing_pct)

    discounted_gen = sum(g / (1 + target_irr) ** (t + 1) for t, g in enumerate(gen))
    discounted_costs = sum((o + debt_payment) / (1 + target_irr) ** (t + 1) for t, o in enumerate(opex))
    expected_price = (equity_capex + discounted_costs) / discounted_gen

    assert result["ppa_price_gbp_mwh"] == pytest.approx(expected_price, abs=0.01)
    assert result["achieved_irr"] == pytest.approx(target_irr, abs=1e-5)


def test_infeasible_when_target_irr_unreachable_even_at_price_ceiling():
    a = _assumptions(opex_per_mw_year=1_000_000.0)  # opex dwarfs any realistic revenue
    with pytest.raises(InfeasiblePrice, match="not achievable"):
        solve_ppa_price(a, target_irr=0.5, target="project", price_bounds=(0.0, 500.0))


def test_infeasible_when_target_already_met_at_price_floor():
    # Deliberately cheap/generous project + a low target + a raised price floor,
    # so even the floor price already clears the target -- the PPA isn't needed.
    a = _assumptions(capex_per_mw=100_000.0, opex_per_mw_year=0.0)
    with pytest.raises(InfeasiblePrice, match="already met"):
        solve_ppa_price(a, target_irr=0.02, target="project", price_bounds=(50.0, 500.0))


def test_rejects_unknown_target():
    a = _assumptions()
    with pytest.raises(ValueError, match="target must be"):
        solve_ppa_price(a, target_irr=0.08, target="nonsense")


def test_discount_rate_reports_npv_without_affecting_solved_price():
    a = _assumptions()
    with_discount = solve_ppa_price(a, target_irr=0.08, target="project", discount_rate=0.05)
    without_discount = solve_ppa_price(a, target_irr=0.08, target="project")

    assert with_discount["ppa_price_gbp_mwh"] == pytest.approx(without_discount["ppa_price_gbp_mwh"])
    assert with_discount["npv_at_discount_rate_gbp"] is not None
    assert without_discount["npv_at_discount_rate_gbp"] is None
    # discount rate (5%) < target IRR (8%) the price was solved for -> positive NPV at that lower hurdle
    assert with_discount["npv_at_discount_rate_gbp"] > 0
