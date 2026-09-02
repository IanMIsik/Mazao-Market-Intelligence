import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.metrics import IncompleteWeekError, _best_1h_spread, build_week  # noqa: E402
from gbpw.settlement import week_dates  # noqa: E402
from gbpw.storage import PriceRow, connect, upsert_prices  # noqa: E402

WEEK_ENDING = date(2026, 8, 30)


def test_best_1h_spread_simple_case():
    # lowest two: 10, 20 (mean 15); highest two: 80, 90 (mean 85); spread = 70
    values = [50, 10, 80, 30, 90, 20, 60]
    assert _best_1h_spread(values) == 70


def test_best_1h_spread_all_equal_is_zero():
    assert _best_1h_spread([42.0] * 48) == 0


def _seed_full_week(conn, dates, day_ahead_by_day):
    """Insert 48 periods/day for every required series across `dates`."""
    for i, d in enumerate(dates):
        da = day_ahead_by_day[i]
        rows = []
        for sp in range(1, 49):
            rows.append(PriceRow("day_ahead", d, sp, "NA", da[sp - 1]))
            rows.append(PriceRow("imbalance", d, sp, "latest", da[sp - 1] + 5))
            rows.append(PriceRow("wind", d, sp, "NA", 3000.0))
            rows.append(PriceRow("total_generation", d, sp, "NA", 10000.0))
            rows.append(PriceRow("demand", d, sp, "NA", 25000.0))
        upsert_prices(conn, rows)


def test_build_week_happy_path(tmp_path):
    conn = connect(tmp_path / "test.db")
    dates = week_dates(WEEK_ENDING)
    # flat £50/MWh every period except one day with a spike, so spread/peak are predictable
    day_ahead_by_day = [[50.0] * 48 for _ in range(7)]
    day_ahead_by_day[2][10] = 200.0  # Wednesday, one very high period
    day_ahead_by_day[2][11] = 200.0
    day_ahead_by_day[2][0] = 5.0
    day_ahead_by_day[2][1] = 5.0

    _seed_full_week(conn, dates, day_ahead_by_day)

    facts = build_week(conn, WEEK_ENDING)

    assert facts["week_ending"] == WEEK_ENDING.isoformat()
    assert len(facts["days"]) == 7
    assert all(d["periods"] == 48 for d in facts["days"])

    wed = facts["days"][2]
    assert wed["day_ahead_peak"] == 200.0
    assert wed["day_ahead_trough"] == 5.0
    assert wed["best_spread"] == 195.0  # mean(200,200) - mean(5,5)

    # wind share: 3000 / 10000 = 30% every period -> 30% every day and for the week
    assert facts["kpi"]["wind_share_pct"] == 30
    assert all(d["wind_share_pct"] == 30.0 for d in facts["days"])

    # highest imbalance = day-ahead + 5, so the Wednesday 200 periods -> 205
    assert facts["kpi"]["highest_imbalance_value"] == 205.0

    # periods above £100: only the two 200 periods on Wednesday
    assert facts["totals"]["periods_above_100_week"] == 2


def test_build_week_raises_on_missing_periods(tmp_path):
    conn = connect(tmp_path / "test.db")
    dates = week_dates(WEEK_ENDING)
    day_ahead_by_day = [[50.0] * 48 for _ in range(7)]
    _seed_full_week(conn, dates, day_ahead_by_day)

    # delete one period from one day to simulate a gap
    conn.execute(
        "DELETE FROM prices WHERE series='day_ahead' AND sd=? AND sp=48",
        (dates[3].isoformat(),),
    )
    conn.commit()

    with pytest.raises(IncompleteWeekError):
        build_week(conn, WEEK_ENDING)
