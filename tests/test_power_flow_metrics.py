import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import power_flow_metrics as pfm  # noqa: E402
from gbpw.storage import FuelInstRow, NA_RUN, PriceRow, connect, upsert_fuelinst, upsert_prices  # noqa: E402

TODAY = date(2026, 9, 17)


def test_current_flow_headline_figures_none_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = pfm.current_flow(conn, TODAY)

    assert result["bm_generation_mw"] is None
    assert result["total_demand_mw"] is None
    assert result["gb_production_mw"] is None
    assert result["net_demand_mw"] is None
    assert result["other_demand_mw"] is None
    assert result["imports_mw"] == 0.0
    assert result["exports_mw"] == 0.0
    assert result["import_legs"] == []
    assert result["export_legs"] == []
    assert result["pumped_storage_charge_mw"] == 0.0


def test_current_flow_gb_production_adds_embedded_wind_and_solar(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("total_generation", TODAY, 21, NA_RUN, 20000.0),
        PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 3000.0),
        PriceRow("solar", TODAY, 21, NA_RUN, 500.0),
    ])

    result = pfm.current_flow(conn, TODAY)

    assert result["bm_generation_mw"] == 20000.0
    assert result["gb_production_mw"] == 23500.0


def test_current_flow_gb_production_still_works_without_embedded_readings(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [PriceRow("total_generation", TODAY, 21, NA_RUN, 20000.0)])

    result = pfm.current_flow(conn, TODAY)

    assert result["gb_production_mw"] == 20000.0


def test_current_flow_splits_interconnector_legs_into_imports_and_exports(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("interconnector_ifa_actual", TODAY, 21, NA_RUN, 500.0),    # France, import
        PriceRow("interconnector_nemo_actual", TODAY, 21, NA_RUN, -300.0),  # Belgium, export
    ])

    result = pfm.current_flow(conn, TODAY)

    assert result["imports_mw"] == 500.0
    assert result["exports_mw"] == 300.0
    assert result["import_legs"] == [{"key": "ifa", "name": "IFA", "country": "FR", "value": 500.0}]
    assert result["export_legs"] == [{"key": "nemo", "name": "Nemo", "country": "BE", "value": 300.0}]


def test_current_flow_total_demand_uses_itsdo_plus_embedded_wind_and_solar(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("demand_itsdo", TODAY, 21, NA_RUN, 24000.0),
        PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 3000.0),
        PriceRow("solar", TODAY, 21, NA_RUN, 9000.0),
        # A raw INDO row is deliberately NOT what feeds total_demand_mw --
        # live-verified INDO doesn't reconcile against GB Production the
        # way ITSDO + embedded generation does (see the module docstring).
        PriceRow("demand", TODAY, 21, NA_RUN, 999999.0),
    ])

    result = pfm.current_flow(conn, TODAY)

    assert result["total_demand_mw"] == 36000.0


def test_current_flow_net_and_other_demand_account_for_exports_and_ps_charge(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_prices(conn, [
        PriceRow("demand_itsdo", TODAY, 21, NA_RUN, 30000.0),
        PriceRow("interconnector_nemo_actual", TODAY, 21, NA_RUN, -300.0),  # export
    ])
    upsert_fuelinst(conn, [FuelInstRow(t, "PS", -900.0)])  # charging

    result = pfm.current_flow(conn, TODAY)

    assert result["total_demand_mw"] == 30000.0
    assert result["pumped_storage_charge_mw"] == 900.0
    assert result["net_demand_mw"] == 30000.0 - 300.0
    assert result["other_demand_mw"] == 30000.0 - 300.0 - 900.0


def test_current_flow_ps_discharging_does_not_count_as_a_charge(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "PS", 400.0)])  # discharging (generating)

    result = pfm.current_flow(conn, TODAY)

    assert result["pumped_storage_charge_mw"] == 0.0


def test_current_flow_other_demand_never_negative(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_prices(conn, [
        PriceRow("demand_itsdo", TODAY, 21, NA_RUN, 100.0),
        PriceRow("interconnector_nemo_actual", TODAY, 21, NA_RUN, -300.0),  # export exceeds demand
    ])
    upsert_fuelinst(conn, [FuelInstRow(t, "PS", -900.0)])

    result = pfm.current_flow(conn, TODAY)

    assert result["other_demand_mw"] == 0.0


def test_current_flow_bm_mix_empty_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert pfm.current_flow(conn, TODAY)["bm_mix"] == []


def test_current_flow_bm_mix_excludes_embedded_wind_and_negative_contributors(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(t, "CCGT", 1000.0),
        FuelInstRow(t, "WIND", 2000.0),
        FuelInstRow(t, "PS", -900.0),  # charging -- can't be a donut slice
    ])
    # If this leaked into bm_mix's WIND figure, it would double-count
    # against the diagram's separate "LV Wind" ring/figure.
    upsert_prices(conn, [PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 5000.0)])

    result = pfm.current_flow(conn, TODAY)

    assert {r["fuel_type"]: r["generation_mw"] for r in result["bm_mix"]} == {"CCGT": 1000.0, "WIND": 2000.0}
