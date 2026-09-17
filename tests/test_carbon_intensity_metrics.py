import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import carbon_intensity_metrics as cim  # noqa: E402
from gbpw.storage import (  # noqa: E402
    CarbonIntensityFactorRow,
    CarbonIntensityRow,
    FuelInstRow,
    NA_RUN,
    PriceRow,
    connect,
    upsert_carbon_intensity,
    upsert_carbon_intensity_factors,
    upsert_fuelinst,
    upsert_prices,
)

TODAY = date(2026, 9, 17)


def test_current_intensity_none_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert cim.current_intensity(conn, TODAY) is None


def test_current_intensity_returns_latest_row_for_today(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_carbon_intensity(conn, CarbonIntensityRow(TODAY, 20, forecast=40.0, actual=42.0, index_label="low"))
    upsert_carbon_intensity(conn, CarbonIntensityRow(TODAY, 21, forecast=35.0, actual=34.0, index_label="low"))

    result = cim.current_intensity(conn, TODAY)

    assert result == {"sd": TODAY.isoformat(), "sp": 21, "forecast": 35.0, "actual": 34.0, "index_label": "low"}


def test_emissions_mix_empty_when_no_factors_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "COAL", 1000.0)])
    # No carbon_intensity_factors rows at all -- must be an honest empty
    # list, not a mix with a fabricated/guessed factor.
    assert cim.emissions_mix(conn, TODAY) == []


def test_emissions_mix_weights_by_factor_not_generation_share(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(t, "COAL", 100.0),   # small generation, high CI
        FuelInstRow(t, "WIND", 10000.0),  # huge generation, zero CI
    ])
    upsert_carbon_intensity_factors(conn, [
        CarbonIntensityFactorRow("COAL", 937.0),
        CarbonIntensityFactorRow("WIND", 0.0),
    ])

    result = {s["key"]: s for s in cim.emissions_mix(conn, TODAY)}

    # Wind generates 100x more MW but contributes zero emissions -- coal
    # alone accounts for essentially all of the emissions share despite
    # being the smaller generator.
    assert "WIND" not in result  # zero factor -> zero emissions -> excluded (nothing to show)
    assert result["COAL"]["emissions"] == 100.0 * 937.0
    assert result["COAL"]["pct"] == 100.0


def test_emissions_mix_includes_solar_with_its_own_factor(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [FuelInstRow(t, "COAL", 100.0)])
    upsert_prices(conn, [PriceRow("solar", TODAY, 21, NA_RUN, 500.0)])
    upsert_carbon_intensity_factors(conn, [
        CarbonIntensityFactorRow("COAL", 937.0),
        CarbonIntensityFactorRow("SOLAR", 0.0),
    ])

    result = {s["key"]: s for s in cim.emissions_mix(conn, TODAY)}

    assert "SOLAR" not in result  # zero factor -> zero emissions, correctly excluded
    assert result["COAL"]["pct"] == 100.0


def test_emissions_mix_splits_imports_by_country_with_published_factor(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("interconnector_ifa_actual", TODAY, 21, NA_RUN, 500.0),   # France
        PriceRow("interconnector_ifa2_actual", TODAY, 21, NA_RUN, 300.0),  # France
    ])
    upsert_carbon_intensity_factors(conn, [CarbonIntensityFactorRow("IMPORT_FR", 53.0)])

    result = cim.emissions_mix(conn, TODAY)

    assert result == [{
        "kind": "import", "key": "FR", "label": "France imports",
        "generation_mw": 800.0, "factor": 53.0, "emissions": 800.0 * 53.0, "pct": 100.0,
    }]


def test_emissions_mix_groups_unpublished_import_countries_as_other_imports(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("interconnector_nsl_actual", TODAY, 21, NA_RUN, 500.0),      # Norway -- no published factor
        PriceRow("interconnector_vikinglink_actual", TODAY, 21, NA_RUN, 300.0),  # Denmark -- no published factor
    ])
    upsert_carbon_intensity_factors(conn, [CarbonIntensityFactorRow("OTHER", 300.0)])

    result = cim.emissions_mix(conn, TODAY)

    assert result == [{
        "kind": "other_import", "key": "OTHER_IMPORTS", "label": "Other imports",
        "generation_mw": 800.0, "factor": 300.0, "emissions": 800.0 * 300.0, "pct": 100.0,
    }]


def test_emissions_mix_sorted_by_emissions_descending(tmp_path):
    conn = connect(tmp_path / "test.db")
    t = datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)
    upsert_fuelinst(conn, [
        FuelInstRow(t, "CCGT", 1000.0),
        FuelInstRow(t, "BIOMASS", 5000.0),
    ])
    upsert_carbon_intensity_factors(conn, [
        CarbonIntensityFactorRow("CCGT", 394.0),
        CarbonIntensityFactorRow("BIOMASS", 120.0),
    ])

    result = cim.emissions_mix(conn, TODAY)

    # CCGT: 1000*394=394,000 vs Biomass: 5000*120=600,000 -- biomass has
    # more generation AND more total emissions here despite the lower
    # per-unit factor, so it should sort first.
    assert [s["key"] for s in result] == ["BIOMASS", "CCGT"]
