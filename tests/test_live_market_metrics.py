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


def test_delta_vs_yesterday_compares_same_settlement_period(tmp_path):
    conn = connect(tmp_path / "test.db")
    today, yesterday = date(2026, 9, 16), date(2026, 9, 15)
    upsert_prices(conn, [
        PriceRow("imbalance", yesterday, 3, "latest", 100.0),
        PriceRow("imbalance", today, 3, "latest", 142.0),
    ])

    assert lmm.delta_vs_yesterday(conn, "imbalance", today) == 42.0


def test_delta_vs_yesterday_none_when_today_has_no_data(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert lmm.delta_vs_yesterday(conn, "imbalance", date(2026, 9, 16)) is None


def test_delta_vs_yesterday_none_when_yesterday_missing_that_period(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [PriceRow("imbalance", today, 3, "latest", 142.0)])

    assert lmm.delta_vs_yesterday(conn, "imbalance", today) is None


def test_actual_vs_forecast_pairs_matching_periods(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("wind", today, 1, "NA", 3000.0),
        PriceRow("wind", today, 2, "NA", 3200.0),
        PriceRow("wind_forecast", today, 1, "2026-09-16T00:00:00Z", 2900.0),
        PriceRow("wind_forecast", today, 2, "2026-09-16T00:00:00Z", 3100.0),
        # Forecast reaches further ahead than the actual has cleared yet.
        PriceRow("wind_forecast", today, 3, "2026-09-16T00:00:00Z", 3300.0),
    ])

    result = lmm.actual_vs_forecast(conn, "wind", "wind_forecast", today)

    assert result["latest_actual"] == 3200.0
    assert result["latest_forecast"] == 3100.0
    assert result["latest_sp"] == 2
    assert result["points"] == [
        {"sp": 1, "actual": 3000.0, "forecast": 2900.0},
        {"sp": 2, "actual": 3200.0, "forecast": 3100.0},
        {"sp": 3, "actual": None, "forecast": 3300.0},
    ]


def test_actual_vs_forecast_uses_latest_published_vintage(tmp_path):
    # series_for_week()'s MAX(run) resolution should pick the later publish.
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("demand", today, 1, "NA", 24000.0),
        PriceRow("demand_forecast", today, 1, "2026-09-15T12:00:00Z", 23000.0),
        PriceRow("demand_forecast", today, 1, "2026-09-16T06:00:00Z", 23800.0),
    ])

    result = lmm.actual_vs_forecast(conn, "demand", "demand_forecast", today)

    assert result["points"] == [{"sp": 1, "actual": 24000.0, "forecast": 23800.0}]


def test_actual_vs_forecast_empty_when_nothing_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = lmm.actual_vs_forecast(conn, "wind", "wind_forecast", date(2026, 9, 16))
    assert result == {"latest_actual": None, "latest_forecast": None, "latest_sp": None, "points": []}


def test_actual_plus_addon_vs_forecast_adds_curtailment(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("wind", today, 1, "NA", 3000.0),
        PriceRow("wind", today, 2, "NA", 3200.0),
        PriceRow("wind_curtailed_mw", today, 1, "NA", 500.0),
        # No curtailment row for SP2 yet (publish lag hasn't elapsed) --
        # must show as missing, not be silently treated as zero.
        PriceRow("wind_forecast", today, 1, "2026-09-16T00:00:00Z", 4000.0),
        PriceRow("wind_forecast", today, 2, "2026-09-16T00:00:00Z", 4100.0),
    ])

    result = lmm.actual_plus_addon_vs_forecast(conn, "wind", "wind_curtailed_mw", "wind_forecast", today)

    assert result["points"] == [
        {"sp": 1, "actual": 3500.0, "forecast": 4000.0},
        {"sp": 2, "actual": None, "forecast": 4100.0},
    ]
    # "Latest" is the rightmost period the combined figure can actually
    # speak to (SP1), not wind's own latest_sp (SP2, addon not published yet).
    assert result["latest_actual"] == 3500.0
    assert result["latest_sp"] == 1
    assert result["latest_forecast"] == 4000.0


def test_actual_plus_addon_vs_forecast_empty_when_nothing_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = lmm.actual_plus_addon_vs_forecast(conn, "wind", "wind_curtailed_mw", "wind_forecast", date(2026, 9, 16))
    assert result == {"latest_actual": None, "latest_forecast": None, "latest_sp": None, "points": []}


def test_actual_and_addon_vs_forecast_merges_all_three(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("wind", today, 1, "NA", 3000.0),
        PriceRow("wind", today, 2, "NA", 3200.0),
        PriceRow("wind_curtailed_mw", today, 1, "NA", 500.0),
        # No curtailment row for SP2 yet -- "combined" must be None there
        # even though "actual" has a real value; the two solid lines end
        # at different points on purpose.
        PriceRow("wind_forecast", today, 1, "2026-09-16T00:00:00Z", 4000.0),
        PriceRow("wind_forecast", today, 2, "2026-09-16T00:00:00Z", 4100.0),
        PriceRow("wind_forecast", today, 3, "2026-09-16T00:00:00Z", 4200.0),  # forecast-only period
    ])

    result = lmm.actual_and_addon_vs_forecast(conn, "wind", "wind_curtailed_mw", "wind_forecast", today)

    assert result == {"points": [
        {"sp": 1, "actual": 3000.0, "combined": 3500.0, "forecast": 4000.0},
        {"sp": 2, "actual": 3200.0, "combined": None, "forecast": 4100.0},
        {"sp": 3, "actual": None, "combined": None, "forecast": 4200.0},
    ]}


def test_actual_and_addon_vs_forecast_empty_when_nothing_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = lmm.actual_and_addon_vs_forecast(conn, "wind", "wind_curtailed_mw", "wind_forecast", date(2026, 9, 16))
    assert result == {"points": []}


def test_dual_series_today_pairs_by_settlement_period(tmp_path):
    conn = connect(tmp_path / "test.db")
    today = date(2026, 9, 16)
    upsert_prices(conn, [
        PriceRow("imbalance", today, 1, "latest", 145.2),
        PriceRow("imbalance", today, 2, "latest", 150.0),
        PriceRow("imbalance_volume", today, 1, "latest", -320.5),
        # SP2's volume hasn't cleared yet -- price for SP2 must still show, paired with None.
        PriceRow("imbalance_volume", today, 3, "latest", 210.0),  # volume-only period
    ])

    result = lmm.dual_series_today(conn, "imbalance", "imbalance_volume", today)

    assert result == {"points": [
        {"sp": 1, "a": 145.2, "b": -320.5},
        {"sp": 2, "a": 150.0, "b": None},
        {"sp": 3, "a": None, "b": 210.0},
    ]}


def test_dual_series_today_empty_when_nothing_yet(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = lmm.dual_series_today(conn, "imbalance", "imbalance_volume", date(2026, 9, 16))
    assert result == {"points": []}
