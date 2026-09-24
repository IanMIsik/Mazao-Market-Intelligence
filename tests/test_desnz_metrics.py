import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import desnz_metrics as dm  # noqa: E402
from gbpw.storage import (  # noqa: E402
    NA_RUN, DesnzPriceScenarioRow, GdpDeflatorRow, PriceRow, connect, upsert_desnz_price_scenarios,
    upsert_gdp_deflator, upsert_prices,
)


def _deflator_rows(years: dict[int, float], vintage="2026-06"):
    return [GdpDeflatorRow(year=y, vintage=vintage, deflator=d, is_forecast=False) for y, d in years.items()]


def test_deflate_applies_ratio():
    index = {2024: 96.5146, 2026: 102.1830881350604}
    # Hand-verified during planning: 76.91 (2024 prices) -> ~81.4 in 2026 money.
    assert dm._deflate(76.91, 2024, 2026, index) == pytest.approx(81.43, abs=0.01)


def test_deflate_returns_none_when_a_year_is_missing_from_index():
    assert dm._deflate(76.91, 2024, 2026, {2024: 96.5146}) is None
    assert dm._deflate(76.91, 2024, 2026, {2026: 102.18}) is None


def test_resolve_money_year_uses_current_year_when_present():
    assert dm._resolve_money_year({2025: 100.0, 2026: 102.18}, date(2026, 6, 1)) == 2026


def test_resolve_money_year_clamps_to_latest_available_year():
    assert dm._resolve_money_year({2025: 100.0, 2026: 102.18, 2030: 110.2}, date(2033, 1, 1)) == 2030


def test_resolve_money_year_falls_back_to_today_when_index_empty():
    assert dm._resolve_money_year({}, date(2026, 6, 1)) == 2026


def test_outturn_by_year_deflates_each_year_by_its_own_index(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", date(2024, 1, 1), 1, NA_RUN, 50.0),
        PriceRow("imrp", date(2024, 6, 1), 1, NA_RUN, 50.0),  # 2024 mean = 50.0
        PriceRow("imrp", date(2026, 1, 1), 1, NA_RUN, 100.0),  # 2026 mean = 100.0, already this year's money
    ])
    index = {2024: 96.5146, 2026: 102.1830881350604}

    out = dm.outturn_by_year(conn, 2026, index)

    by_year = {p["year"]: p["value"] for p in out}
    assert by_year[2026] == 100.0
    assert by_year[2024] == dm._deflate(50.0, 2024, 2026, index)


def test_outturn_by_year_omits_a_year_the_deflator_doesnt_cover(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [PriceRow("imrp", date(2010, 1, 1), 1, NA_RUN, 40.0)])

    out = dm.outturn_by_year(conn, 2026, {2026: 102.18})  # no 2010 entry
    assert out == []


def test_scenarios_for_vintage_rebases_by_single_ratio(tmp_path):
    # `to_year` is always the one global money_year applied uniformly
    # across the whole vintage (see long_term_outlook()) -- a 2030
    # scenario reading gets rebased into 2026 money exactly like a 2026
    # one, both by the same deflator[2026]/deflator[2024] ratio, not by
    # a per-row deflator[row's own year]/deflator[2024] ratio (that
    # would double-count the scenario's own real-terms growth as if it
    # were inflation).
    conn = connect(tmp_path / "test.db")
    upsert_desnz_price_scenarios(conn, [
        DesnzPriceScenarioRow("2026-02", "reference", 2026, 76.91, 2024),
        DesnzPriceScenarioRow("2026-02", "ffp_low", 2030, 46.51, 2024),
        DesnzPriceScenarioRow("2026-02", "ffp_high", 2030, 76.64, 2024),
    ])
    index = {2024: 96.5146, 2026: 102.1830881350604, 2030: 110.23343069925426}

    out = dm.scenarios_for_vintage(conn, "2026-02", 2026, index)
    # Hand-verified during planning: reference ~81.4, FFP low/high ~49.2/81.2, all in 2026 money.
    assert out["reference"][0]["value"] == pytest.approx(81.43, abs=0.01)
    assert out["ffp_low"][0]["value"] == pytest.approx(49.24, abs=0.01)
    assert out["ffp_high"][0]["value"] == pytest.approx(81.15, abs=0.02)


def test_scenarios_for_vintage_ratio_is_uniform_regardless_of_the_rows_own_year(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_desnz_price_scenarios(conn, [
        DesnzPriceScenarioRow("2026-02", "reference", 2026, 100.0, 2024),
        DesnzPriceScenarioRow("2026-02", "ffp_low", 2050, 100.0, 2024),  # same raw value, a different year
    ])
    index = {2024: 96.5146, 2026: 102.1830881350604, 2050: 200.0}

    out = dm.scenarios_for_vintage(conn, "2026-02", 2026, index)
    # Both rows carry the same raw value and price_base_year, so both must rebase identically --
    # the row's OWN year (2026 vs 2050) plays no part in the ratio.
    assert out["reference"][0]["value"] == out["ffp_low"][0]["value"]


def test_scenarios_for_vintage_empty_when_price_base_year_not_in_index(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_desnz_price_scenarios(conn, [DesnzPriceScenarioRow("2026-02", "reference", 2026, 76.91, 2024)])

    out = dm.scenarios_for_vintage(conn, "2026-02", 2026, {2026: 102.18})  # no 2024 entry
    assert out["reference"] == []


def test_available_vintages_sorted(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_desnz_price_scenarios(conn, [
        DesnzPriceScenarioRow("2026-02", "reference", 2026, 76.91, 2024),
        DesnzPriceScenarioRow("2024-12", "reference", 2026, 70.0, 2024),
    ])
    assert dm.available_vintages(conn) == ["2024-12", "2026-02"]


def test_long_term_outlook_no_data(tmp_path):
    conn = connect(tmp_path / "test.db")
    out = dm.long_term_outlook(conn, today=date(2026, 6, 1))

    assert out["has_data"] is False
    assert out["vintage"] is None
    assert out["chart_data"] == {"labels": [], "outturn": [], "reference": [], "ffp_low": [], "ffp_high": []}


def test_long_term_outlook_uses_latest_vintage_and_states_money_year(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_gdp_deflator(conn, _deflator_rows({2024: 96.5146, 2026: 102.1830881350604}))
    upsert_desnz_price_scenarios(conn, [
        DesnzPriceScenarioRow("2024-12", "reference", 2026, 70.0, 2024),
        DesnzPriceScenarioRow("2026-02", "reference", 2026, 76.91, 2024),
        DesnzPriceScenarioRow("2026-02", "ffp_low", 2026, 60.0, 2024),
        DesnzPriceScenarioRow("2026-02", "ffp_high", 2026, 90.0, 2024),
    ])
    upsert_prices(conn, [PriceRow("imrp", date(2026, 1, 1), 1, NA_RUN, 101.9)])

    out = dm.long_term_outlook(conn, today=date(2026, 6, 1))

    assert out["has_data"] is True
    assert out["vintage"] == "2026-02"
    assert out["other_vintages"] == ["2024-12"]
    assert out["money_year"] == 2026
    assert out["money_year_clamped"] is False
    assert out["desnz_base_year"] == 2024
    cd = out["chart_data"]
    i = cd["labels"].index(2026)
    assert cd["reference"][i] == pytest.approx(81.43, abs=0.01)
    assert cd["outturn"][i] == 101.9


def test_long_term_outlook_clamps_money_year_beyond_deflator_horizon(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_gdp_deflator(conn, _deflator_rows({2024: 96.5146, 2030: 110.23343069925426}))
    upsert_desnz_price_scenarios(conn, [DesnzPriceScenarioRow("2026-02", "reference", 2030, 60.3, 2024)])

    out = dm.long_term_outlook(conn, today=date(2033, 1, 1))

    assert out["money_year"] == 2030
    assert out["money_year_clamped"] is True
