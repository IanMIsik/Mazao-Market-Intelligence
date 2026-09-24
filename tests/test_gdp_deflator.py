import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import gdp_deflator  # noqa: E402


def test_build_index_real_levels_pass_through_unchanged():
    raw = {2023: (92.8675, 6.3538), 2024: (96.5146, 3.9272), 2025: (100.0, 3.6113)}
    index, forecast_years = gdp_deflator._build_index(raw)

    assert index == {2023: 92.8675, 2024: 96.5146, 2025: 100.0}
    assert forecast_years == set()


def test_build_index_compounds_forecast_years_forward_from_last_real_level():
    raw = {2025: (100.0, 3.6113), 2026: (None, 2.183088135060407), 2027: (None, 1.944412716488264)}
    index, forecast_years = gdp_deflator._build_index(raw)

    assert index[2025] == 100.0
    assert round(index[2026], 3) == 102.183
    assert round(index[2027], 3) == round(102.183 * 1.01944412716488264, 3)
    assert forecast_years == {2026, 2027}


def test_build_index_skips_a_year_with_neither_level_nor_resolvable_prior_year():
    # 2026 has a %-change but no prior year (2025) in `raw` at all -- can't compound, so it's left out.
    raw = {2026: (None, 2.18)}
    index, forecast_years = gdp_deflator._build_index(raw)

    assert index == {}
    assert forecast_years == set()


def test_build_index_real_verified_numbers_from_june_2026_release():
    # Hand-verified against the real downloaded file during planning -- 2024=96.5146, 2025=100,
    # 2026 %-change=2.183088135060407 -> 2026 index should land at 102.183.
    raw = {2024: (96.5146, 3.9272081190944026), 2025: (100.0, 3.6112671036299155), 2026: (None, 2.183088135060407)}
    index, forecast_years = gdp_deflator._build_index(raw)

    assert index[2024] == 96.5146
    assert index[2025] == 100.0
    assert round(index[2026], 3) == 102.183
    assert forecast_years == {2026}


def test_extract_calendar_year_series_handles_footnoted_forecast_year_label():
    class FakeSheet:
        def iter_rows(self, values_only=True):
            base = (None, "1955-56", 3.0588, None, 19575, 19590, None)
            yield base + ("1955", 3.037, None)
            yield base + ("2026 (1), (2)", "-", 2.183088135060407)

    raw = gdp_deflator._extract_calendar_year_series(FakeSheet())
    assert raw[1955] == (3.037, None)
    assert raw[2026] == (None, 2.183088135060407)
