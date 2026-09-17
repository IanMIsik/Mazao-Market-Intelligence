import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import ppa_metrics  # noqa: E402
from gbpw.storage import NA_RUN, PriceRow, connect, upsert_prices  # noqa: E402

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
