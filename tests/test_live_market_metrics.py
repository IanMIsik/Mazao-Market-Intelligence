import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import live_market_metrics as lmm  # noqa: E402
from gbpw.storage import PriceRow, connect, upsert_prices  # noqa: E402


def test_week_so_far_monday_through_yesterday():
    # Wed 16 Sep 2026 -- most recently completed Sunday is 13 Sep, so the
    # in-progress week started Mon 14 Sep, and "so far" runs through Tue 15.
    assert lmm.week_so_far(date(2026, 9, 16)) == (date(2026, 9, 14), date(2026, 9, 15))


def test_week_so_far_none_on_monday():
    # The week just started -- no complete trailing days yet.
    assert lmm.week_so_far(date(2026, 9, 14)) is None


def test_day_stats_computes_avg_peak_trough_per_day(tmp_path):
    conn = connect(tmp_path / "test.db")
    d1, d2 = date(2026, 9, 14), date(2026, 9, 15)
    upsert_prices(conn, [
        PriceRow("day_ahead", d1, 1, "NA", 100.0),
        PriceRow("day_ahead", d1, 2, "NA", 200.0),
        PriceRow("day_ahead", d2, 1, "NA", 50.0),
    ])

    stats = lmm.day_stats(conn, "day_ahead", [d1, d2])

    assert stats[0] == {"date": "2026-09-14", "avg": 150.0, "peak": 200.0, "trough": 100.0, "periods": 2}
    assert stats[1] == {"date": "2026-09-15", "avg": 50.0, "peak": 50.0, "trough": 50.0, "periods": 1}


def test_day_stats_honest_about_a_day_with_no_data(tmp_path):
    # A genuinely missing day must show as missing, not be skipped or
    # crash on an empty mean -- same honesty convention as the rest of
    # this codebase (e.g. the day-ahead completeness disclosure).
    conn = connect(tmp_path / "test.db")
    d1, d2 = date(2026, 9, 14), date(2026, 9, 15)
    upsert_prices(conn, [PriceRow("day_ahead", d1, 1, "NA", 100.0)])

    stats = lmm.day_stats(conn, "day_ahead", [d1, d2])

    assert stats[1] == {"date": "2026-09-15", "avg": None, "peak": None, "trough": None, "periods": 0}


def test_day_stats_empty_dates_returns_empty_list(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert lmm.day_stats(conn, "day_ahead", []) == []


def test_today_progression_returns_latest_and_all_points(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("wind", today, 1, "NA", 3000.0),
        PriceRow("wind", today, 3, "NA", 3500.0),
        PriceRow("wind", today, 2, "NA", 3200.0),
    ])

    result = lmm.today_progression(conn, "wind", today)

    assert result["latest_value"] == 3500.0
    assert result["latest_sp"] == 3
    assert result["points"] == [
        {"sp": 1, "value": 3000.0}, {"sp": 2, "value": 3200.0}, {"sp": 3, "value": 3500.0},
    ]


def test_today_progression_empty_when_nothing_published_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = lmm.today_progression(conn, "wind", date(2026, 9, 16))
    assert result == {"latest_value": None, "latest_sp": None, "points": []}
