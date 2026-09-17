import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import fuelinst  # noqa: E402

WINDOW_START = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 17, 10, 35, tzinfo=timezone.utc)


def test_interconnector_fuel_types_are_excluded(monkeypatch):
    def fake_get(url, params):
        return [
            {"publishTime": "2026-09-17T10:05:00Z", "startTime": "2026-09-17T10:05:00Z",
             "settlementDate": "2026-09-17", "settlementPeriod": 21, "fuelType": "WIND", "generation": 3000.0},
            {"publishTime": "2026-09-17T10:05:00Z", "startTime": "2026-09-17T10:05:00Z",
             "settlementDate": "2026-09-17", "settlementPeriod": 21, "fuelType": "INTFR", "generation": 500.0},
        ]

    monkeypatch.setattr(fuelinst, "_get", fake_get)
    rows, note = fuelinst.fetch_fuelinst(WINDOW_START, WINDOW_END)

    assert len(rows) == 1
    assert rows[0].fuel_type == "WIND"
    assert rows[0].generation_mw == 3000.0
    assert "ok (1 rows)" == note


def test_later_publish_for_same_period_overwrites_earlier_one(monkeypatch):
    def fake_get(url, params):
        return [
            {"publishTime": "2026-09-17T10:00:00Z", "startTime": "2026-09-17T10:05:00Z",
             "settlementDate": "2026-09-17", "settlementPeriod": 21, "fuelType": "CCGT", "generation": 1000.0},
            {"publishTime": "2026-09-17T10:10:00Z", "startTime": "2026-09-17T10:05:00Z",
             "settlementDate": "2026-09-17", "settlementPeriod": 21, "fuelType": "CCGT", "generation": 1200.0},
        ]

    monkeypatch.setattr(fuelinst, "_get", fake_get)
    rows, _note = fuelinst.fetch_fuelinst(WINDOW_START, WINDOW_END)

    assert len(rows) == 1
    assert rows[0].generation_mw == 1200.0


def test_start_time_parsed_as_utc_aware(monkeypatch):
    def fake_get(url, params):
        return [{"publishTime": "2026-09-17T10:05:00Z", "startTime": "2026-09-17T10:05:00Z",
                  "settlementDate": "2026-09-17", "settlementPeriod": 21, "fuelType": "NUCLEAR", "generation": 5000.0}]

    monkeypatch.setattr(fuelinst, "_get", fake_get)
    rows, _note = fuelinst.fetch_fuelinst(WINDOW_START, WINDOW_END)

    assert rows[0].start_time == datetime(2026, 9, 17, 10, 5, tzinfo=timezone.utc)


def test_empty_response_returns_empty_ok(monkeypatch):
    monkeypatch.setattr(fuelinst, "_get", lambda url, params: [])
    rows, note = fuelinst.fetch_fuelinst(WINDOW_START, WINDOW_END)
    assert rows == []
    assert note == "ok (0 rows)"
