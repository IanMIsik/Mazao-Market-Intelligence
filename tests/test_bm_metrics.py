import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import bm_metrics  # noqa: E402
from gbpw.storage import (  # noqa: E402
    BmCashflowRow,
    BmUnitReferenceRow,
    EacRow,
    connect,
    upsert_bm_cashflows,
    upsert_bm_unit_reference,
    upsert_eac_results,
)

START = date(2026, 9, 8)
END = date(2026, 9, 10)  # 3-day window


def _eac_row(**overrides):
    base = dict(
        neso_id=1, unit_result_id="u1", service_type="Response", auction_product="DCL",
        technology_type="Batteries", auction_unit="AUNIT01", participant="Alpha Energy",
        executed_quantity=10.0, clearing_price=5.0,
        delivery_start="2026-09-08T00:00:00", delivery_end="2026-09-08T00:30:00",
        sd=START, sp=1, post_code=None,
    )
    base.update(overrides)
    return EacRow(**base)


def _cf_row(**overrides):
    base = dict(sd=START, sp=1, national_grid_bm_unit="AUNIT01", bid_offer="offer", total_cashflow=0.0)
    base.update(overrides)
    return BmCashflowRow(**base)


def _seed(conn):
    # Battery units identified via EAC (only auction_unit + technology_type matter here)
    upsert_eac_results(conn, [
        _eac_row(neso_id=1, auction_unit="AUNIT01", technology_type="Batteries", participant="Alpha Energy"),
        _eac_row(neso_id=2, auction_unit="AUNIT02", technology_type="Batteries", participant="Beta Storage"),
        _eac_row(neso_id=3, auction_unit="AUNIT04", technology_type="Wind", participant="Delta Wind"),
    ])

    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("AUNIT01", "T_AUNIT01", "Alpha Co", "T", 10.0),
        BmUnitReferenceRow("AUNIT02", "T_AUNIT02", "Beta Co", "T", 0.0),  # capacity unknown/zero
        BmUnitReferenceRow("AUNIT04", "T_AUNIT04", "Delta Co", "T", 20.0),
        # AUNIT99 has cashflow below but no bm_unit_reference row at all -- LEFT JOIN must still work
    ])

    upsert_bm_cashflows(conn, [
        _cf_row(national_grid_bm_unit="AUNIT01", bid_offer="offer", total_cashflow=10.0),
        _cf_row(national_grid_bm_unit="AUNIT01", bid_offer="bid", total_cashflow=-2.0),
        _cf_row(national_grid_bm_unit="AUNIT02", bid_offer="offer", total_cashflow=5.0),
        _cf_row(national_grid_bm_unit="AUNIT04", bid_offer="offer", total_cashflow=100.0),  # not a battery
        _cf_row(national_grid_bm_unit="AUNIT99", bid_offer="offer", total_cashflow=50.0),  # no matching EAC row
    ])


def test_battery_bm_units_scoped_to_technology(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    assert set(bm_metrics.battery_bm_units(conn)) == {"AUNIT01", "AUNIT02"}
    assert set(bm_metrics.battery_bm_units(conn, technology_type=None)) == {"AUNIT01", "AUNIT02", "AUNIT04"}


def test_leaderboard_excludes_units_without_matching_eac_row(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    all_units = {e["national_grid_bm_unit"] for e in activity["leaderboard"] + activity["leaderboard_no_capacity"]}
    assert "AUNIT04" not in all_units  # not a battery
    assert "AUNIT99" not in all_units  # no EAC row at all


def test_bid_and_offer_cashflows_sum_correctly(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    aunit01 = next(e for e in activity["leaderboard"] if e["national_grid_bm_unit"] == "AUNIT01")
    assert aunit01["total_revenue_gbp"] == 8.0  # -2.0 (bid) + 10.0 (offer)


def test_bid_and_offer_revenue_kept_separate_not_just_netted(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    assert activity["total_bid_revenue_gbp"] == -2.0
    assert activity["total_offer_revenue_gbp"] == 15.0  # 10.0 (AUNIT01) + 5.0 (AUNIT02)
    aunit01 = next(e for e in activity["leaderboard"] if e["national_grid_bm_unit"] == "AUNIT01")
    assert aunit01["bid_revenue_gbp"] == -2.0
    assert aunit01["offer_revenue_gbp"] == 10.0
    aunit02 = next(e for e in activity["leaderboard_no_capacity"] if e["national_grid_bm_unit"] == "AUNIT02")
    assert aunit02["bid_revenue_gbp"] == 0.0
    assert aunit02["offer_revenue_gbp"] == 5.0


def test_gbp_per_mw_per_day_none_when_capacity_zero_or_missing(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    aunit02 = next(e for e in activity["leaderboard_no_capacity"] if e["national_grid_bm_unit"] == "AUNIT02")
    assert aunit02["gbp_per_mw_per_day"] is None
    assert aunit02["total_revenue_gbp"] == 5.0  # revenue still shown even without capacity


def test_gbp_per_mw_per_day_computed_when_capacity_known(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    aunit01 = next(e for e in activity["leaderboard"] if e["national_grid_bm_unit"] == "AUNIT01")
    days = (END - START).days + 1
    assert aunit01["gbp_per_mw_per_day"] == 8.0 / 10.0 / days


def test_units_with_and_without_capacity_counts(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    assert activity["units_with_capacity"] == 1
    assert activity["units_without_capacity"] == 1
    assert activity["total_revenue_gbp"] == 13.0  # 8.0 (AUNIT01) + 5.0 (AUNIT02)


def test_median_gbp_per_mw_day_uses_only_units_with_known_capacity(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    activity = bm_metrics.bm_activity(conn, START, END)
    # Only AUNIT01 has known capacity in this fixture -- median of one value is that value.
    days = (END - START).days + 1
    assert activity["median_gbp_per_mw_day"] == 8.0 / 10.0 / days


def test_unit_detail_includes_units_with_no_cashflow_at_all(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    detail = bm_metrics.unit_detail(conn, ["AUNIT01", "AUNIT02", "AUNIT03"], START, END)
    assert set(detail.keys()) == {"AUNIT01", "AUNIT02", "AUNIT03"}
    assert detail["AUNIT03"]["total_revenue_gbp"] == 0.0
    assert detail["AUNIT03"]["gbp_per_mw_per_day"] is None
    assert detail["AUNIT01"]["bid_revenue_gbp"] == -2.0
    assert detail["AUNIT01"]["offer_revenue_gbp"] == 10.0


def test_empty_range_returns_zeros_not_error(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    future_start, future_end = date(2030, 1, 1), date(2030, 1, 7)
    activity = bm_metrics.bm_activity(conn, future_start, future_end)
    assert activity["total_revenue_gbp"] == 0.0
    assert activity["leaderboard"] == []
    assert activity["leaderboard_no_capacity"] == []
    assert activity["units_with_capacity"] == 0
    assert activity["units_without_capacity"] == 0


def test_no_matching_battery_units_returns_zeros(tmp_path):
    conn = connect(tmp_path / "test.db")
    # no EAC rows seeded at all -- no batteries identified
    upsert_bm_cashflows(conn, [_cf_row(national_grid_bm_unit="AUNIT01", bid_offer="offer", total_cashflow=999.0)])
    activity = bm_metrics.bm_activity(conn, START, END)
    assert activity["leaderboard"] == []
    assert activity["total_revenue_gbp"] == 0.0
