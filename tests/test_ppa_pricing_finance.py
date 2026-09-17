import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ppa_pricing import finance  # noqa: E402


def test_npv_zero_rate_is_plain_sum():
    assert finance.npv(0.0, [-100.0, 40.0, 40.0, 40.0]) == pytest.approx(20.0)


def test_npv_matches_hand_discounted_example():
    # -100 + 110/1.1 = 0
    assert finance.npv(0.10, [-100.0, 110.0]) == pytest.approx(0.0, abs=1e-6)


def test_irr_single_period_exact():
    assert finance.irr([-100.0, 110.0]) == pytest.approx(0.10, abs=1e-6)


def test_irr_multi_year_satisfies_npv_zero():
    cashflows = [-1000.0, 200.0, 200.0, 200.0, 200.0, 200.0, 200.0]
    rate = finance.irr(cashflows)
    assert finance.npv(rate, cashflows) == pytest.approx(0.0, abs=1e-6)


def test_irr_rejects_non_negative_initial_cashflow():
    with pytest.raises(ValueError, match="negative cashflow at year 0"):
        finance.irr([0.0, 100.0])


def test_irr_rejects_multiple_sign_changes():
    with pytest.raises(ValueError, match="one sign change"):
        finance.irr([-100.0, 50.0, -50.0, 200.0])


def test_irr_rejects_cashflow_that_never_turns_positive():
    with pytest.raises(ValueError, match="one sign change"):
        finance.irr([-100.0, -10.0, -10.0])
