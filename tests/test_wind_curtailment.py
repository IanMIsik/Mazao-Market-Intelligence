import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import wind_curtailment  # noqa: E402

D = date(2026, 9, 17)


def test_completely_empty_response_is_not_yet_published_not_zero(monkeypatch):
    # A period Elexon hasn't published at all comes back with zero rows
    # total (confirmed live) -- must be skipped, not stored as a
    # "confirmed zero curtailment."
    def fake_fetch(d, sp):
        return [] if sp == 3 else [{"id": "T_MOWEO-1", "volume": -10.0}]

    monkeypatch.setattr(wind_curtailment, "_fetch_bid_stack", fake_fetch)
    rows, note = wind_curtailment.fetch_wind_curtailment(D, [1, 2, 3], {"T_MOWEO-1"})

    assert {r.sp for r in rows} == {1, 2}
    assert "not yet published: [3]" in note


def test_published_period_with_no_wind_rows_is_a_genuine_zero(monkeypatch):
    # Published (non-empty full stack) but nothing matches a wind unit --
    # that's a real, confirmed zero, not a publish-lag gap.
    def fake_fetch(d, sp):
        return [{"id": "OTHER_UNIT", "volume": -5.0}]

    monkeypatch.setattr(wind_curtailment, "_fetch_bid_stack", fake_fetch)
    rows, note = wind_curtailment.fetch_wind_curtailment(D, [1], {"T_MOWEO-1"})

    assert len(rows) == 1
    assert rows[0].sp == 1
    assert rows[0].value == 0.0
    assert "not yet published" not in note


def test_sums_and_doubles_volume_across_matching_wind_units(monkeypatch):
    def fake_fetch(d, sp):
        return [
            {"id": "T_MOWEO-1", "volume": -10.0},
            {"id": "T_MOWEO-1", "volume": -5.0},
            {"id": "T_MOWEO-2", "volume": -20.0},
            {"id": "NOT_WIND", "volume": -100.0},
        ]

    monkeypatch.setattr(wind_curtailment, "_fetch_bid_stack", fake_fetch)
    rows, _note = wind_curtailment.fetch_wind_curtailment(D, [1], {"T_MOWEO-1", "T_MOWEO-2"})

    assert rows[0].value == (10.0 + 5.0 + 20.0) * 2


def test_failed_fetch_is_skipped_not_stored_as_zero(monkeypatch):
    def fake_fetch(d, sp):
        raise RuntimeError("boom")

    monkeypatch.setattr(wind_curtailment, "_fetch_bid_stack", fake_fetch)
    rows, note = wind_curtailment.fetch_wind_curtailment(D, [1], {"T_MOWEO-1"})

    assert rows == []
    assert "failed: [1]" in note


def test_no_wind_units_known_skips_entirely():
    rows, note = wind_curtailment.fetch_wind_curtailment(D, [1, 2], set())
    assert rows == []
    assert "no WIND units known" in note


def test_no_periods_returns_ok_empty():
    rows, note = wind_curtailment.fetch_wind_curtailment(D, [], {"T_MOWEO-1"})
    assert rows == []
    assert note == "ok (0 periods)"
