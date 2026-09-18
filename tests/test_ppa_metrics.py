import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import ppa_metrics  # noqa: E402
from gbpw.storage import (  # noqa: E402
    NA_RUN, CfdAuctionOutcomeRow, PriceRow, connect, upsert_cfd_auction_outcomes, upsert_prices,
)

D1 = date(2026, 1, 1)
D2 = date(2026, 1, 2)


def test_capture_price_none_when_no_generation(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = ppa_metrics.capture_price(conn, "wind", D1, D2)

    assert result["capture_price_gbp_mwh"] is None
    assert result["baseload_price_gbp_mwh"] is None
    assert result["capture_rate_pct"] is None
    assert result["total_generation_mwh"] == 0.0


def test_capture_price_volume_weighted_against_imrp(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", D1, 1, NA_RUN, 100.0),
        PriceRow("imrp", D1, 2, NA_RUN, 50.0),
        PriceRow("wind", D1, 1, NA_RUN, 1000.0),  # generates a lot when price is high
        PriceRow("wind", D1, 2, NA_RUN, 0.0),     # generates nothing when price is low
    ])

    result = ppa_metrics.capture_price(conn, "wind", D1, D1)

    # capture price = weighted avg = 100 (all generation in period 1)
    assert result["capture_price_gbp_mwh"] == 100.0
    # baseload = plain average of 100 and 50 = 75
    assert result["baseload_price_gbp_mwh"] == 75.0
    # capture rate = 100/75*100 = 133.3%
    assert result["capture_rate_pct"] == pytest.approx(133.3, abs=0.1)
    assert result["total_generation_mwh"] == 500.0  # 1000 MW * 0.5h


def test_capture_price_baseload_ignores_price_periods_the_technology_has_no_data_for(tmp_path):
    # Regression test for a real bug: baseload must come from the SAME
    # (sd, sp) rows joined for capture price, not a separate query over
    # every IMRP period in the window -- otherwise, mid-backfill (or any
    # time the two series simply aren't ingested in lockstep), IMRP's
    # extra, unmatched periods (here: a second, much more expensive day
    # solar has no data for at all) leak into "baseload" and distort the
    # ratio in a way that has nothing to do with solar's real exposure.
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", D1, 1, NA_RUN, 100.0),
        PriceRow("solar", D1, 1, NA_RUN, 10.0),
        # D2's IMRP price is much higher, but solar has NO data for D2 at
        # all (not yet backfilled) -- it must not count toward baseload.
        PriceRow("imrp", D2, 1, NA_RUN, 1000.0),
    ])

    result = ppa_metrics.capture_price(conn, "solar", D1, D2)

    assert result["baseload_price_gbp_mwh"] == 100.0  # not (100+1000)/2 = 550
    assert result["capture_price_gbp_mwh"] == 100.0
    assert result["capture_rate_pct"] == 100.0


def test_capture_price_by_month_baseload_scoped_per_month_to_matched_rows(tmp_path):
    conn = connect(tmp_path / "test.db")
    jan = date(2026, 1, 15)
    upsert_prices(conn, [
        PriceRow("imrp", jan, 1, NA_RUN, 100.0),
        PriceRow("solar", jan, 1, NA_RUN, 10.0),
        # Same month, but a period solar has no reading for at all -- its
        # much higher price must not leak into January's baseload.
        PriceRow("imrp", jan, 2, NA_RUN, 900.0),
    ])

    rows = ppa_metrics.capture_price_by_month(conn, "solar", date(2026, 1, 1), date(2026, 1, 31))

    assert rows[0]["baseload_price_gbp_mwh"] == 100.0


def test_capture_price_rejects_unknown_technology(tmp_path):
    conn = connect(tmp_path / "test.db")
    with pytest.raises(ValueError):
        ppa_metrics.capture_price(conn, "nuclear", D1, D2)


def test_capture_price_by_month_buckets_correctly(tmp_path):
    conn = connect(tmp_path / "test.db")
    jan = date(2026, 1, 15)
    feb = date(2026, 2, 15)
    upsert_prices(conn, [
        PriceRow("imrp", jan, 1, NA_RUN, 100.0),
        PriceRow("solar", jan, 1, NA_RUN, 10.0),
        PriceRow("imrp", feb, 1, NA_RUN, 200.0),
        PriceRow("solar", feb, 1, NA_RUN, 20.0),
    ])

    rows = ppa_metrics.capture_price_by_month(conn, "solar", date(2026, 1, 1), date(2026, 2, 28))

    assert [r["month"] for r in rows] == ["2026-01", "2026-02"]
    assert rows[0]["capture_price_gbp_mwh"] == 100.0
    assert rows[1]["capture_price_gbp_mwh"] == 200.0


def test_capture_price_by_month_omits_months_with_no_generation(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", date(2026, 1, 15), 1, NA_RUN, 100.0),
        PriceRow("solar", date(2026, 1, 15), 1, NA_RUN, 10.0),
        # February has an IMRP price but no solar generation at all -- not a fabricated 0%.
        PriceRow("imrp", date(2026, 2, 15), 1, NA_RUN, 200.0),
    ])

    rows = ppa_metrics.capture_price_by_month(conn, "solar", date(2026, 1, 1), date(2026, 2, 28))

    assert [r["month"] for r in rows] == ["2026-01"]


def test_capture_rate_chart_data_empty_when_no_months():
    assert ppa_metrics.capture_rate_chart_data([], []) == {"labels": [], "wind": [], "solar": []}


def test_capture_rate_chart_data_fills_contiguous_months():
    wind_by_month = [
        {"month": "2026-01", "capture_rate_pct": 95.0},
        {"month": "2026-03", "capture_rate_pct": 101.0},
    ]
    result = ppa_metrics.capture_rate_chart_data(wind_by_month, [])

    # Feb is missing from the source rows entirely, but must still occupy
    # its own slot in the contiguous month range -- a real gap, not
    # compressed away.
    assert result["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert result["wind"] == [95.0, None, 101.0]
    assert result["solar"] == [None, None, None]


def test_capture_rate_chart_data_wind_and_solar_independently_aligned():
    wind_by_month = [{"month": "2026-01", "capture_rate_pct": 90.0}]
    solar_by_month = [{"month": "2026-02", "capture_rate_pct": 80.0}]

    result = ppa_metrics.capture_rate_chart_data(wind_by_month, solar_by_month)

    assert result["labels"] == ["2026-01", "2026-02"]
    assert result["wind"] == [90.0, None]
    assert result["solar"] == [None, 80.0]


def test_available_years_empty_when_no_imrp_data(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert ppa_metrics.available_years(conn) == []


def test_available_years_spans_earliest_to_latest_imrp_year(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", date(2023, 11, 1), 1, NA_RUN, 50.0),
        PriceRow("imrp", date(2026, 2, 1), 1, NA_RUN, 60.0),
    ])
    # contiguous, including years with no IMRP row at all in between (2024, 2025)
    assert ppa_metrics.available_years(conn) == [2023, 2024, 2025, 2026]


def test_available_years_single_year_when_all_data_in_one_year(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("imrp", date(2026, 1, 1), 1, NA_RUN, 50.0),
        PriceRow("imrp", date(2026, 6, 1), 1, NA_RUN, 60.0),
    ])
    assert ppa_metrics.available_years(conn) == [2026]


def test_curtailment_risk_none_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = ppa_metrics.curtailment_risk(conn, D1, D2)

    assert result["curtailed_mwh"] == 0.0
    assert result["actual_mwh"] == 0.0
    assert result["curtailed_pct"] is None


def test_curtailment_risk_computes_pct_of_potential_output(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("wind", D1, 1, NA_RUN, 900.0),
        PriceRow("wind_curtailed_mw", D1, 1, NA_RUN, 100.0),
    ])

    result = ppa_metrics.curtailment_risk(conn, D1, D1)

    assert result["actual_mwh"] == 450.0  # 900 MW * 0.5h
    assert result["curtailed_mwh"] == 50.0  # 100 MW * 0.5h
    assert result["potential_mwh"] == 500.0
    assert result["curtailed_pct"] == 10.0


def _cfd_row(**overrides):
    base = dict(
        lccc_id=1, auction="AR6", project_name="Test Solar Farm", developer="Test Developer",
        technology_type="Solar PV (> 5MW)", capacity_mw=20.0, strike_price_gbp_mwh=50.0,
        price_base_year=2012, delivery_year="2026", region="Scotland", publication_date="2024-09-03",
    )
    base.update(overrides)
    return CfdAuctionOutcomeRow(**base)


def test_cfd_category_matches_both_solar_spelling_variants():
    assert ppa_metrics._cfd_category("Solar PV (> 5MW)") == "solar"
    assert ppa_metrics._cfd_category("Solar Photo-Voltaic (>5MW)") == "solar"


def test_cfd_category_matches_wind_variants_and_ignores_others():
    assert ppa_metrics._cfd_category("Onshore Wind (> 5MW)") == "wind"
    assert ppa_metrics._cfd_category("Offshore Wind") == "wind"
    assert ppa_metrics._cfd_category("Floating Offshore Wind") == "wind"
    assert ppa_metrics._cfd_category("Remote Island Wind (>5MW)") == "wind"
    assert ppa_metrics._cfd_category("Dedicated Biomass with CHP") is None


def test_cfd_benchmark_empty_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert ppa_metrics.cfd_benchmark(conn, "solar") == []


def test_cfd_benchmark_capacity_weighted_average_per_round(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_cfd_auction_outcomes(conn, [
        _cfd_row(lccc_id=1, auction="AR6", capacity_mw=10.0, strike_price_gbp_mwh=40.0),
        _cfd_row(lccc_id=2, auction="AR6", capacity_mw=30.0, strike_price_gbp_mwh=60.0),
        _cfd_row(lccc_id=3, auction="AR6", technology_type="Onshore Wind (> 5MW)"),  # different category
    ])

    result = ppa_metrics.cfd_benchmark(conn, "solar")

    assert len(result) == 1
    r = result[0]
    assert r["auction"] == "AR6"
    assert r["project_count"] == 2
    assert r["total_capacity_mw"] == 40.0
    # (10*40 + 30*60) / 40 = 55.0
    assert r["avg_strike_price_gbp_mwh"] == 55.0
    assert r["price_base_year"] == 2012


def test_cfd_benchmark_sorts_rounds_numerically_not_alphabetically(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_cfd_auction_outcomes(conn, [
        _cfd_row(lccc_id=1, auction="AR10", price_base_year=2024),
        _cfd_row(lccc_id=2, auction="AR2"),
        _cfd_row(lccc_id=3, auction="AR1"),
    ])

    result = ppa_metrics.cfd_benchmark(conn, "solar")

    # alphabetically "AR1" < "AR10" < "AR2" -- must not sort that way
    assert [r["auction"] for r in result] == ["AR1", "AR2", "AR10"]
