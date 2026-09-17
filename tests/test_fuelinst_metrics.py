import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import fuelinst_metrics as fim  # noqa: E402
from gbpw.storage import FuelInstRow, NA_RUN, PriceRow, connect, upsert_fuelinst, upsert_prices  # noqa: E402

# Real interconnector keys/countries from ingest/elexon.py's
# INTERCONNECTORS dict, not invented: ifa/ifa2/eleclink=France (grouped
# into one "France imports" segment -- see GROUPED_IMPORT_COUNTRY),
# nemo=Belgium, britned=Netherlands (neither grouped), nsl=Norway (a
# LOW_CARBON_IMPORT_COUNTRIES link, also used as an export leg below),
# moyle=N.Ireland (used as an export leg below).

TODAY = date(2026, 9, 17)


def test_current_mix_empty_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert fim.current_mix(conn, TODAY) == {"start_time": None, "by_fuel": []}


def test_current_mix_uses_only_the_latest_start_time(tmp_path):
    conn = connect(tmp_path / "test.db")
    earlier = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    latest = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(earlier, "CCGT", 1000.0),
        FuelInstRow(latest, "CCGT", 1200.0),
        FuelInstRow(latest, "NUCLEAR", 5000.0),
    ])

    result = fim.current_mix(conn, TODAY)

    assert result["start_time"] == latest.isoformat()
    assert result["by_fuel"] == [
        {"fuel_type": "NUCLEAR", "generation_mw": 5000.0},
        {"fuel_type": "CCGT", "generation_mw": 1200.0},
    ]


def test_current_mix_adds_wind_curtailment_at_the_matching_settlement_period(tmp_path):
    conn = connect(tmp_path / "test.db")
    # 10:05 UTC on 17 Sep 2026 (BST, UTC+1) is 11:05 local -> SP23.
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    upsert_prices(conn, [PriceRow("wind_curtailed_mw", TODAY, 23, NA_RUN, 500.0)])

    result = fim.current_mix(conn, TODAY)

    assert result["by_fuel"] == [{"fuel_type": "WIND", "generation_mw": 3500.0}]


def test_current_mix_wind_falls_back_to_raw_when_curtailment_not_yet_published(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    # No wind_curtailed_mw row at all for this settlement period.

    result = fim.current_mix(conn, TODAY)

    assert result["by_fuel"] == [{"fuel_type": "WIND", "generation_mw": 3000.0}]


def test_current_mix_adds_embedded_wind_forecast_on_top(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    upsert_prices(conn, [PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 400.0)])

    result = fim.current_mix(conn, TODAY)

    assert result["by_fuel"] == [{"fuel_type": "WIND", "generation_mw": 3400.0}]


def test_current_mix_wind_unaffected_when_no_embedded_forecast_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    # No wind_embedded_forecast row at all.

    result = fim.current_mix(conn, TODAY)

    assert result["by_fuel"] == [{"fuel_type": "WIND", "generation_mw": 3000.0}]


def test_current_mix_include_embedded_wind_false_excludes_it(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 3000.0)])
    upsert_prices(conn, [PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 400.0)])

    result = fim.current_mix(conn, TODAY, include_embedded_wind=False)

    assert result["by_fuel"] == [{"fuel_type": "WIND", "generation_mw": 3000.0}]


def test_current_mix_embedded_wind_does_not_affect_other_fuel_types(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "CCGT", 1000.0)])
    upsert_prices(conn, [PriceRow("wind_embedded_forecast", TODAY, 21, NA_RUN, 400.0)])

    result = fim.current_mix(conn, TODAY)

    assert result["by_fuel"] == [{"fuel_type": "CCGT", "generation_mw": 1000.0}]


def test_current_mix_by_category_empty_returns_all_four_categories_at_zero(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert fim.current_mix_by_category(conn, TODAY) == [
        {"category": "Fossil Fuels", "generation_mw": 0.0, "segments": []},
        {"category": "Renewables", "generation_mw": 0.0, "segments": []},
        {"category": "Low Carbon", "generation_mw": 0.0, "segments": []},
        {"category": "Other", "generation_mw": 0.0, "segments": []},
    ]


def test_current_mix_by_category_buckets_fuel_types(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(t, "CCGT", 1000.0),   # Fossil Fuels
        FuelInstRow(t, "WIND", 2000.0),   # Renewables
        FuelInstRow(t, "NUCLEAR", 3000.0),  # Low Carbon
        FuelInstRow(t, "BIOMASS", 400.0),  # Other
        FuelInstRow(t, "PS", -100.0),      # Other (charging, net-negative)
    ])

    by_category = {r["category"]: r for r in fim.current_mix_by_category(conn, TODAY)}

    assert by_category["Fossil Fuels"]["generation_mw"] == 1000.0
    assert by_category["Renewables"]["generation_mw"] == 2000.0
    assert by_category["Low Carbon"]["generation_mw"] == 3000.0
    # Net total includes the PS charging (-100), but the drawn segments
    # only ever carry positive contributors -- a negative one can't be
    # stacked as a slice, same rule the donuts already apply.
    assert by_category["Other"]["generation_mw"] == 300.0
    assert by_category["Other"]["segments"] == [{"kind": "fuel", "key": "BIOMASS", "label": None, "generation_mw": 400.0}]

    assert by_category["Fossil Fuels"]["segments"] == [{"kind": "fuel", "key": "CCGT", "label": None, "generation_mw": 1000.0}]
    assert by_category["Renewables"]["segments"] == [{"kind": "fuel", "key": "WIND", "label": None, "generation_mw": 2000.0}]


def test_current_mix_by_category_segments_sorted_largest_first(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(t, "CCGT", 500.0),
        FuelInstRow(t, "OCGT", 100.0),
        FuelInstRow(t, "COAL", 900.0),
    ])

    by_category = {r["category"]: r for r in fim.current_mix_by_category(conn, TODAY)}

    keys = [s["key"] for s in by_category["Fossil Fuels"]["segments"]]
    assert keys == ["COAL", "CCGT", "OCGT"]


def test_current_mix_by_category_adds_solar_as_its_own_renewables_segment(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "WIND", 2000.0)])
    upsert_prices(conn, [PriceRow("solar", TODAY, 21, NA_RUN, 500.0)])

    by_category = {r["category"]: r for r in fim.current_mix_by_category(conn, TODAY)}

    assert by_category["Renewables"]["generation_mw"] == 2500.0
    assert {"kind": "solar", "key": "SOLAR", "label": None, "generation_mw": 500.0} in by_category["Renewables"]["segments"]


def test_current_mix_by_category_buckets_import_legs_by_country_and_excludes_exports(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("interconnector_nsl_actual", TODAY, 21, NA_RUN, 500.0),   # Norway, import -> Low Carbon
        PriceRow("interconnector_nemo_actual", TODAY, 21, NA_RUN, 200.0),  # Belgium, import -> Other
        PriceRow("interconnector_moyle_actual", TODAY, 21, NA_RUN, -300.0),  # N.Ireland, export -> excluded
    ])

    by_category = {r["category"]: r for r in fim.current_mix_by_category(conn, TODAY)}

    assert by_category["Low Carbon"]["generation_mw"] == 500.0
    assert by_category["Low Carbon"]["segments"] == [
        {"kind": "import", "key": "nsl", "label": "North Sea Link", "generation_mw": 500.0}
    ]
    assert by_category["Other"]["generation_mw"] == 200.0
    assert by_category["Other"]["segments"] == [{"kind": "import", "key": "nemo", "label": "Nemo", "generation_mw": 200.0}]


def test_current_mix_by_category_groups_france_interconnectors_into_one_segment(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("interconnector_ifa_actual", TODAY, 21, NA_RUN, 500.0),       # France, import
        PriceRow("interconnector_ifa2_actual", TODAY, 21, NA_RUN, 300.0),      # France, import
        PriceRow("interconnector_eleclink_actual", TODAY, 21, NA_RUN, 200.0),  # France, import
        PriceRow("interconnector_britned_actual", TODAY, 21, NA_RUN, 100.0),  # Netherlands, import -> not grouped
    ])

    by_category = {r["category"]: r for r in fim.current_mix_by_category(conn, TODAY)}

    # All 3 French legs land in Low Carbon (France is in
    # LOW_CARBON_IMPORT_COUNTRIES) and are summed into a single segment.
    assert by_category["Low Carbon"]["generation_mw"] == 1000.0
    assert by_category["Low Carbon"]["segments"] == [
        {"kind": "import", "key": "france", "label": "France imports", "generation_mw": 1000.0}
    ]
    # A non-French import leg is unaffected -- stays its own segment.
    assert by_category["Other"]["segments"] == [
        {"kind": "import", "key": "britned", "label": "BritNed", "generation_mw": 100.0}
    ]
