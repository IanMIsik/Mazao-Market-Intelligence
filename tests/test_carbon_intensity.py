import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import carbon_intensity  # noqa: E402


def test_fetch_carbon_intensity_parses_headline_figure(monkeypatch):
    def fake_get(path):
        assert path == "/intensity"
        return [{"from": "2026-09-17T12:00Z", "to": "2026-09-17T12:30Z",
                  "intensity": {"forecast": 31, "actual": 34, "index": "low"}}]

    monkeypatch.setattr(carbon_intensity, "_get", fake_get)
    row, note = carbon_intensity.fetch_carbon_intensity()

    assert row.forecast == 31
    assert row.actual == 34
    assert row.index_label == "low"
    assert "low" in note


def test_fetch_carbon_intensity_factors_maps_to_our_own_keys(monkeypatch):
    def fake_get(path):
        assert path == "/intensity/factors"
        return [{
            "Biomass": 120, "Coal": 937, "Dutch Imports": 474, "French Imports": 53,
            "Gas (Combined Cycle)": 394, "Gas (Open Cycle)": 651, "Hydro": 0,
            "Irish Imports": 458, "Nuclear": 0, "Oil": 935, "Other": 300,
            "Pumped Storage": 0, "Solar": 0, "Wind": 0,
        }]

    monkeypatch.setattr(carbon_intensity, "_get", fake_get)
    rows, note = carbon_intensity.fetch_carbon_intensity_factors()

    by_key = {r.key: r.factor for r in rows}
    assert by_key == {
        "BIOMASS": 120.0, "COAL": 937.0, "IMPORT_NL": 474.0, "IMPORT_FR": 53.0,
        "CCGT": 394.0, "OCGT": 651.0, "NPSHYD": 0.0, "IMPORT_IRL": 458.0,
        "NUCLEAR": 0.0, "OIL": 935.0, "OTHER": 300.0, "PS": 0.0, "SOLAR": 0.0, "WIND": 0.0,
    }
    assert "14 factors" in note


def test_fetch_carbon_intensity_factors_skips_unmapped_keys(monkeypatch):
    def fake_get(path):
        return [{"Wind": 0, "Some New Fuel NESO Adds Later": 42}]

    monkeypatch.setattr(carbon_intensity, "_get", fake_get)
    rows, note = carbon_intensity.fetch_carbon_intensity_factors()

    assert {r.key for r in rows} == {"WIND"}
    assert "1 factors" in note
