import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.storage import BmUnitReferenceRow, connect, upsert_bm_unit_reference, wind_elexon_units  # noqa: E402


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
