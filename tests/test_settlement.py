import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.settlement import periods_in_date, sp_start_utc, utc_to_settlement, week_dates  # noqa: E402


def test_normal_day_has_48_periods():
    assert periods_in_date(date(2026, 8, 26)) == 48


def test_spring_forward_has_46_periods():
    # UK clocks go forward on the last Sunday of March -- 2026-03-29.
    assert periods_in_date(date(2026, 3, 29)) == 46


def test_autumn_back_has_50_periods():
    # UK clocks go back on the last Sunday of October -- 2026-10-25.
    assert periods_in_date(date(2026, 10, 25)) == 50


def test_day_either_side_of_clock_change_is_normal():
    assert periods_in_date(date(2026, 3, 28)) == 48
    assert periods_in_date(date(2026, 3, 30)) == 48
    assert periods_in_date(date(2026, 10, 24)) == 48
    assert periods_in_date(date(2026, 10, 26)) == 48


def test_sp_start_utc_bst_day_sp1_is_23_00_previous_day_utc():
    d = date(2026, 8, 26)  # BST (UTC+1) in effect
    start = sp_start_utc(d, 1)
    assert start.hour == 23
    assert start.date() == d - timedelta(days=1)


def test_sp_start_utc_gmt_day_sp1_is_midnight_utc():
    d = date(2026, 1, 12)  # GMT, no offset
    start = sp_start_utc(d, 1)
    assert start.hour == 0
    assert start.date() == d


def test_sp_start_utc_and_utc_to_settlement_roundtrip_normal_day():
    d = date(2026, 8, 26)
    for sp in range(1, 49):
        dt = sp_start_utc(d, sp)
        rt_date, rt_sp = utc_to_settlement(dt)
        assert (rt_date, rt_sp) == (d, sp)


def test_sp_start_utc_and_utc_to_settlement_roundtrip_spring_forward():
    d = date(2026, 3, 29)
    for sp in range(1, 47):
        dt = sp_start_utc(d, sp)
        rt_date, rt_sp = utc_to_settlement(dt)
        assert (rt_date, rt_sp) == (d, sp)


def test_sp_start_utc_and_utc_to_settlement_roundtrip_autumn_back():
    d = date(2026, 10, 25)
    for sp in range(1, 51):
        dt = sp_start_utc(d, sp)
        rt_date, rt_sp = utc_to_settlement(dt)
        assert (rt_date, rt_sp) == (d, sp)


def test_sp_start_utc_rejects_zero_or_negative():
    with pytest.raises(ValueError):
        sp_start_utc(date(2026, 8, 26), 0)


def test_week_dates_returns_monday_through_sunday():
    dates = week_dates(date(2026, 8, 30))  # a Sunday
    assert dates == [date(2026, 8, d) for d in range(24, 31)]
    assert [d.weekday() for d in dates] == [0, 1, 2, 3, 4, 5, 6]


def test_week_dates_rejects_non_sunday():
    with pytest.raises(ValueError):
        week_dates(date(2026, 8, 29))  # a Saturday
