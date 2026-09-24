import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import desnz_eep  # noqa: E402


def test_rows_from_series_converts_p_per_kwh_to_gbp_per_mwh():
    rows = desnz_eep._rows_from_series("2026-02", "reference", 2024, {2026: 7.691})

    assert len(rows) == 1
    r = rows[0]
    assert r.vintage == "2026-02"
    assert r.scenario == "reference"
    assert r.year == 2026
    assert r.value_gbp_mwh == 76.91
    assert r.price_base_year == 2024


def test_rows_from_series_sorted_by_year():
    rows = desnz_eep._rows_from_series("2026-02", "reference", 2024, {2028: 6.0, 2026: 7.0, 2027: 6.5})
    assert [r.year for r in rows] == [2026, 2027, 2028]


def test_rows_from_series_empty_series_produces_no_rows():
    assert desnz_eep._rows_from_series("2026-02", "reference", 2024, {}) == []


def test_fetch_vintage_rejects_unknown_vintage():
    try:
        desnz_eep.fetch_vintage("1999-01")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "1999-01" in str(e)
