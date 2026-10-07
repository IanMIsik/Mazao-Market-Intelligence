import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.storage import (  # noqa: E402
    BmUnitReferenceRow,
    CarbonIntensityFactorRow,
    CarbonIntensityRow,
    FuelInstRow,
    connect,
    latest_fetch_ts,
    log_fetch,
    upsert_bm_unit_reference,
    upsert_carbon_intensity,
    upsert_carbon_intensity_factors,
    upsert_fuelinst,
    wind_elexon_units,
)


def test_regular_refresh_never_overwrites_an_existing_fuel_type(tmp_path):
    # Simulates the one-off spreadsheet load setting a real fuel type,
    # then a routine background refresh re-running with only the live
    # API's own (often-null) fuelType -- that refresh must not erase it.
    conn = connect(tmp_path / "test.db")
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("ABRTW-1", "E_ABRTW-1", "Wind Co", "T", 50.0, fuel_type="WIND"),
    ], overwrite_fuel_type=True)

    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("ABRTW-1", "E_ABRTW-1", "Wind Co", "T", 50.0, fuel_type=None),
    ])  # overwrite_fuel_type defaults to False

    row = conn.execute("SELECT fuel_type FROM bm_unit_reference WHERE national_grid_bm_unit='ABRTW-1'").fetchone()
    assert row[0] == "WIND"


def test_regular_refresh_fills_a_previously_unknown_fuel_type(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("NEWU-1", "T_NEWU-1", "New Co", "T", 10.0, fuel_type=None),
    ])
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("NEWU-1", "T_NEWU-1", "New Co", "T", 10.0, fuel_type="CCGT"),
    ])
    row = conn.execute("SELECT fuel_type FROM bm_unit_reference WHERE national_grid_bm_unit='NEWU-1'").fetchone()
    assert row[0] == "CCGT"


def test_reload_with_overwrite_flag_replaces_an_existing_fuel_type(tmp_path):
    # The one-off "recreate the list" load is authoritative -- a re-run
    # with a newer spreadsheet must be able to correct a stale value.
    conn = connect(tmp_path / "test.db")
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("UNIT-1", "T_UNIT-1", "Co", "T", 10.0, fuel_type="OTHER"),
    ], overwrite_fuel_type=True)
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("UNIT-1", "T_UNIT-1", "Co", "T", 10.0, fuel_type="WIND"),
    ], overwrite_fuel_type=True)
    row = conn.execute("SELECT fuel_type FROM bm_unit_reference WHERE national_grid_bm_unit='UNIT-1'").fetchone()
    assert row[0] == "WIND"


def test_wind_elexon_units_filters_by_fuel_type_and_excludes_nulls(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_bm_unit_reference(conn, [
        BmUnitReferenceRow("W1", "T_W1", "Wind Co", "T", 50.0, fuel_type="WIND"),
        BmUnitReferenceRow("W2", None, "Wind Co 2", "T", 30.0, fuel_type="WIND"),  # no elexon_bm_unit -- can't join, excluded
        BmUnitReferenceRow("G1", "T_G1", "Gas Co", "T", 100.0, fuel_type="CCGT"),
    ], overwrite_fuel_type=True)

    assert wind_elexon_units(conn) == {"T_W1"}


def test_upsert_fuelinst_overwrites_on_conflict(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3200.0)])  # a later publish revising the same reading

    row = conn.execute(
        "SELECT generation_mw FROM fuelinst_generation WHERE start_time = ? AND fuel_type = 'WIND'",
        (t.isoformat(),),
    ).fetchone()
    assert row[0] == 3200.0


def test_upsert_fuelinst_keeps_different_fuel_types_independent(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0), FuelInstRow(t, "CCGT", 8000.0)])

    rows = conn.execute("SELECT fuel_type, generation_mw FROM fuelinst_generation ORDER BY fuel_type").fetchall()
    assert rows == [("CCGT", 8000.0), ("WIND", 3000.0)]


def test_upsert_carbon_intensity_overwrites_on_conflict(tmp_path):
    conn = connect(tmp_path / "test.db")
    d = date(2026, 9, 17)
    upsert_carbon_intensity(conn, CarbonIntensityRow(d, 21, forecast=40.0, actual=None, index_label="low"))
    upsert_carbon_intensity(conn, CarbonIntensityRow(d, 21, forecast=35.0, actual=34.0, index_label="low"))

    row = conn.execute(
        "SELECT forecast, actual, index_label FROM carbon_intensity WHERE sd = ? AND sp = 21", (d.isoformat(),)
    ).fetchone()
    assert row == (35.0, 34.0, "low")


def test_upsert_carbon_intensity_factors_overwrites_on_conflict(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_carbon_intensity_factors(conn, [CarbonIntensityFactorRow("COAL", 900.0)])
    upsert_carbon_intensity_factors(conn, [CarbonIntensityFactorRow("COAL", 937.0)])

    row = conn.execute("SELECT factor FROM carbon_intensity_factors WHERE key = 'COAL'").fetchone()
    assert row[0] == 937.0


def test_upsert_carbon_intensity_factors_keeps_different_keys_independent(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_carbon_intensity_factors(conn, [CarbonIntensityFactorRow("COAL", 937.0), CarbonIntensityFactorRow("WIND", 0.0)])

    rows = conn.execute("SELECT key, factor FROM carbon_intensity_factors ORDER BY key").fetchall()
    assert rows == [("COAL", 937.0), ("WIND", 0.0)]


def test_latest_fetch_ts_returns_none_when_nothing_logged(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert latest_fetch_ts(conn) is None


def test_latest_fetch_ts_returns_the_max_across_all_series(tmp_path):
    conn = connect(tmp_path / "test.db")
    log_fetch(conn, "wind", date(2026, 9, 17), ok=True, note="ok")
    conn.execute("UPDATE fetch_log SET ts = '2026-09-17T10:00:00+00:00' WHERE series = 'wind'")
    log_fetch(conn, "fuelinst", date(2026, 9, 17), ok=True, note="ok")
    conn.execute("UPDATE fetch_log SET ts = '2026-09-17T10:05:00+00:00' WHERE series = 'fuelinst'")
    conn.commit()

    assert latest_fetch_ts(conn) == "2026-09-17T10:05:00+00:00"


def test_upsert_prices_and_bm_cashflows_skip_unchanged_rows_but_apply_changes(tmp_path):
    # The 5-minute refresh re-fetches whole windows; an unchanged row must
    # not be rewritten (that write amplification made the live server's
    # disk the bottleneck), but a revised value must still land.
    from gbpw.storage import BmCashflowRow, PriceRow, upsert_bm_cashflows, upsert_prices

    conn = connect(tmp_path / "t.db")
    d = date(2026, 1, 1)

    upsert_prices(conn, [PriceRow("x", d, 1, "NA", 1.0)])
    before = conn.total_changes
    upsert_prices(conn, [PriceRow("x", d, 1, "NA", 1.0)])
    assert conn.total_changes == before
    upsert_prices(conn, [PriceRow("x", d, 1, "NA", 2.0)])
    assert conn.execute("SELECT value FROM prices").fetchone()[0] == 2.0

    upsert_bm_cashflows(conn, [BmCashflowRow(d, 1, "U-1", "bid", 5.0)])
    before = conn.total_changes
    upsert_bm_cashflows(conn, [BmCashflowRow(d, 1, "U-1", "bid", 5.0)])
    assert conn.total_changes == before
    upsert_bm_cashflows(conn, [BmCashflowRow(d, 1, "U-1", "bid", 6.0)])
    assert conn.execute("SELECT total_cashflow FROM bm_cashflows").fetchone()[0] == 6.0


def test_series_for_week_returns_latest_run_per_period_and_distinct_periods_counts_once(tmp_path):
    from gbpw.storage import PriceRow, distinct_periods, series_for_week, upsert_prices

    conn = connect(tmp_path / "t.db")
    d, other = date(2026, 1, 1), date(2026, 1, 2)
    upsert_prices(conn, [PriceRow("f", d, sp, "2026-01-01T00:00", 1.0) for sp in (1, 2, 3)])
    # A later run revises periods 1-2 only; period 3 must keep its older run's value.
    upsert_prices(conn, [PriceRow("f", d, sp, "2026-01-01T06:00", 9.0) for sp in (1, 2)])
    upsert_prices(conn, [PriceRow("f", other, 5, "2026-01-01T00:00", 4.0)])
    upsert_prices(conn, [PriceRow("g", d, 1, "NA", 7.0)])

    got = series_for_week(conn, "f", [d, other, date(2026, 1, 3)])

    assert got == {("2026-01-01", 1): 9.0, ("2026-01-01", 2): 9.0, ("2026-01-01", 3): 1.0, ("2026-01-02", 5): 4.0}
    assert distinct_periods(conn, "f", d) == 3
    assert distinct_periods(conn, "f", date(2026, 1, 3)) == 0


def test_prune_superseded_runs_keeps_newest_run_per_period_and_only_touches_forecast_series(tmp_path):
    from gbpw.storage import PriceRow, prune_superseded_runs, series_for_week, upsert_prices

    conn = connect(tmp_path / "t.db")
    d = date(2026, 1, 1)
    upsert_prices(conn, [PriceRow("solar_forecast", d, sp, "2026-01-01T00:00", 1.0) for sp in (1, 2, 3)])
    upsert_prices(conn, [PriceRow("solar_forecast", d, sp, "2026-01-01T06:00", 9.0) for sp in (1, 2)])
    # Not a forecast series: multiple runs are real history there and must survive.
    upsert_prices(conn, [PriceRow("imbalance", d, 1, "II", 1.0), PriceRow("imbalance", d, 1, "SF", 2.0)])
    before = series_for_week(conn, "solar_forecast", [d])

    deleted = prune_superseded_runs(conn)

    assert deleted == 2  # the two replaced 00:00 rows; period 3's only run stays
    assert series_for_week(conn, "solar_forecast", [d]) == before
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE series = 'imbalance'").fetchone()[0] == 2
    assert prune_superseded_runs(conn) == 0  # idempotent
