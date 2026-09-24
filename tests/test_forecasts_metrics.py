import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import forecasts_metrics as fm  # noqa: E402
from gbpw.storage import PriceRow, connect, upsert_prices  # noqa: E402

TODAY = date(2026, 9, 23)
DAY1 = date(2026, 9, 24)
DAY2 = date(2026, 9, 25)
DAY14 = date(2026, 10, 7)


def test_forecast_window_days_spans_tomorrow_through_day_14():
    days = fm.forecast_window_days(TODAY)
    assert len(days) == 14
    assert days[0] == DAY1
    assert days[-1] == DAY14


def test_wind_forecast_all_stitches_day1_from_short_series_and_rest_from_medium(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("wind_forecast", DAY1, 1, "NA", 1000.0),  # day 1, short-term WINDFOR
        PriceRow("wind_forecast_14d", DAY2, 1, "NA", 2000.0),  # day 2, medium-term
        # A day-1 row in the medium-term series must NOT be used -- day 1
        # is always the short-term series, even if the medium-term one
        # happens to carry an (unused) value for that date too.
        PriceRow("wind_forecast_14d", DAY1, 1, "NA", 9999.0),
    ])

    points = fm.wind_forecast(conn, TODAY)

    assert points[0] == {"date": DAY1.isoformat(), "sp": 1, "value": 1000.0}
    assert {"date": DAY2.isoformat(), "sp": 1, "value": 2000.0} in points
    assert not any(p["date"] == DAY1.isoformat() and p["value"] == 9999.0 for p in points)


def test_wind_forecast_single_day_selects_the_right_source_series(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("wind_forecast", DAY1, 1, "NA", 1000.0),
        PriceRow("wind_forecast_14d", DAY2, 1, "NA", 2000.0),
    ])

    day1_points = fm.wind_forecast(conn, TODAY, selected_day=DAY1)
    day2_points = fm.wind_forecast(conn, TODAY, selected_day=DAY2)

    assert day1_points == [{"sp": 1, "value": 1000.0}]
    assert day2_points == [{"sp": 1, "value": 2000.0}]


def test_demand_forecast_uses_elexon_for_day1_once_fully_published(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, _full_day_rows("demand_forecast", DAY1, 25000.0) + [
        PriceRow("demand_forecast_14d", DAY2, 1, "NA", 26000.0),
    ])

    points = fm.demand_forecast(conn, TODAY)

    assert {"date": DAY1.isoformat(), "sp": 1, "value": 25000.0} in points
    assert {"date": DAY2.isoformat(), "sp": 1, "value": 26000.0} in points


def test_demand_forecast_falls_back_to_medium_term_for_incomplete_day1(tmp_path):
    # Elexon's NDF only has a handful of periods published so far for day
    # 1 -- direct request: fall back to demand_forecast_14d (NESO's
    # medium-term product) entirely for that day until NDF is complete,
    # rather than showing a chart that visibly stops partway through.
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("demand_forecast", DAY1, 1, "NA", 99999.0),  # incomplete Elexon reading -- must be ignored
        PriceRow("demand_forecast_14d", DAY1, 1, "NA", 25500.0),
        PriceRow("demand_forecast_14d", DAY1, 2, "NA", 25600.0),
    ])

    points = fm.demand_forecast(conn, TODAY, selected_day=DAY1)

    assert points == [{"sp": 1, "value": 25500.0}, {"sp": 2, "value": 25600.0}]


def test_demand_forecast_switches_back_to_elexon_once_day1_is_complete(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(
        conn,
        _full_day_rows("demand_forecast", DAY1, 25000.0) + [PriceRow("demand_forecast_14d", DAY1, 1, "NA", 99999.0)],
    )

    points = fm.demand_forecast(conn, TODAY, selected_day=DAY1)

    assert points[0] == {"sp": 1, "value": 25000.0}
    assert all(p["value"] == 25000.0 for p in points)


def test_solar_forecast_reads_one_series_across_the_whole_window(tmp_path):
    # Unlike wind/demand, solar has no day-1/days-2-14 split -- the same
    # "solar_forecast" series covers every day.
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("solar_forecast", DAY1, 1, "NA", 0.0),
        PriceRow("solar_forecast", DAY2, 20, "NA", 500.0),
    ])

    points = fm.solar_forecast(conn, TODAY)

    assert {"date": DAY1.isoformat(), "sp": 1, "value": 0.0} in points
    assert {"date": DAY2.isoformat(), "sp": 20, "value": 500.0} in points

    single_day = fm.solar_forecast(conn, TODAY, selected_day=DAY2)
    assert single_day == [{"sp": 20, "value": 500.0}]


def test_wind_forecast_empty_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert fm.wind_forecast(conn, TODAY) == []
    assert fm.wind_forecast(conn, TODAY, selected_day=DAY1) == []


def test_residual_demand_subtracts_wind_from_demand(tmp_path):
    conn = connect(tmp_path / "test.db")
    # Filler rows first (48/48) so Elexon's own day-1 demand is complete
    # and _day1_demand_series() doesn't fall back to demand_forecast_14d
    # -- this test's actual concern is wind subtraction/solar exclusion,
    # not the fallback, so day 1 needs to genuinely be "complete" for it
    # to exercise the right path. Later rows with the same key overwrite
    # the filler (upsert_prices' own ON CONFLICT DO UPDATE, applied in
    # list order).
    rows = _full_day_rows("demand_forecast", DAY1, 20000.0) + _full_day_rows("wind_forecast", DAY1, 5000.0)
    rows += [
        PriceRow("demand_forecast", DAY1, 1, "NA", 25000.0),
        PriceRow("wind_forecast", DAY1, 1, "NA", 8000.0),
        # A real solar reading is present but must be completely ignored --
        # direct request: only wind and nuclear are subtracted, not solar.
        PriceRow("solar_forecast", DAY1, 1, "NA", 2000.0),
        # SP2 has demand and wind but no solar reading at all -- must still
        # be included (solar isn't part of this calculation any more, so
        # its absence can't exclude a period the way wind's absence does).
        PriceRow("demand_forecast", DAY1, 2, "NA", 26000.0),
        PriceRow("wind_forecast", DAY1, 2, "NA", 7000.0),
    ]
    upsert_prices(conn, rows)

    points = fm.residual_demand(conn, TODAY, selected_day=DAY1)

    sp1 = next(p for p in points if p["sp"] == 1)
    sp2 = next(p for p in points if p["sp"] == 2)
    assert sp1 == {"sp": 1, "value": 17000.0}
    assert sp2 == {"sp": 2, "value": 19000.0}


def test_residual_demand_all_days_carries_the_date_field(tmp_path):
    conn = connect(tmp_path / "test.db")
    rows = _full_day_rows("demand_forecast", DAY1, 20000.0) + _full_day_rows("wind_forecast", DAY1, 5000.0)
    rows += [
        PriceRow("demand_forecast", DAY1, 1, "NA", 25000.0),
        PriceRow("wind_forecast", DAY1, 1, "NA", 8000.0),
    ]
    upsert_prices(conn, rows)

    points = fm.residual_demand(conn, TODAY)

    sp1 = next(p for p in points if p["date"] == DAY1.isoformat() and p["sp"] == 1)
    assert sp1 == {"date": DAY1.isoformat(), "sp": 1, "value": 17000.0}


def test_residual_demand_subtracts_nuclear_when_available(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("demand_forecast_14d", DAY2, 1, "NA", 25000.0),
        PriceRow("wind_forecast_14d", DAY2, 1, "NA", 8000.0),
        PriceRow("nuclear_forecast_14d", DAY2, 1, "NA", 4000.0),
    ])

    points = fm.residual_demand(conn, TODAY, selected_day=DAY2)

    assert points == [{"sp": 1, "value": 13000.0}]


def test_residual_demand_day1_unaffected_by_nuclears_missing_coverage(tmp_path):
    # Nuclear's own dataset never reaches day 1 -- residual demand for day
    # 1 must still compute (just without a nuclear deduction), not come
    # back empty because one of its inputs is structurally never there.
    conn = connect(tmp_path / "test.db")
    rows = _full_day_rows("demand_forecast", DAY1, 20000.0) + _full_day_rows("wind_forecast", DAY1, 5000.0)
    rows += [
        PriceRow("demand_forecast", DAY1, 1, "NA", 25000.0),
        PriceRow("wind_forecast", DAY1, 1, "NA", 8000.0),
    ]
    upsert_prices(conn, rows)

    points = fm.residual_demand(conn, TODAY, selected_day=DAY1)

    sp1 = next(p for p in points if p["sp"] == 1)
    assert sp1 == {"sp": 1, "value": 17000.0}


def test_nuclear_forecast_only_covers_days_2_through_14(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [PriceRow("nuclear_forecast_14d", DAY2, 1, "NA", 4053.0)])

    assert fm.nuclear_forecast(conn, TODAY, selected_day=DAY1) == []
    assert fm.nuclear_forecast(conn, TODAY, selected_day=DAY2) == [{"sp": 1, "value": 4053.0}]
    all_points = fm.nuclear_forecast(conn, TODAY)
    assert all_points == [{"date": DAY2.isoformat(), "sp": 1, "value": 4053.0}]


def test_nuclear_forecast_by_day_returns_one_row_per_ingested_day(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_prices(conn, [
        PriceRow("nuclear_forecast_14d", DAY2, 1, "NA", 4053.0),
        PriceRow("nuclear_forecast_14d", DAY2, 2, "NA", 4053.0),  # same day, same broadcast value -- not a second row
        PriceRow("nuclear_forecast_14d", DAY14, 1, "NA", 5136.0),
    ])

    rows = fm.nuclear_forecast_by_day(conn, TODAY)

    assert rows == [{"date": DAY2, "value": 4053.0}, {"date": DAY14, "value": 5136.0}]


def test_nuclear_forecast_by_day_empty_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert fm.nuclear_forecast_by_day(conn, TODAY) == []


def test_stat_summary_computes_max_min_mean():
    points = [{"sp": 1, "value": 10.0}, {"sp": 2, "value": 30.0}, {"sp": 3, "value": 20.0}]
    assert fm.stat_summary(points) == {"max": 30.0, "min": 10.0, "mean": 20.0}


def test_stat_summary_empty_returns_none_fields():
    assert fm.stat_summary([]) == {"max": None, "min": None, "mean": None}


def _full_day_rows(series: str, d: date, value: float) -> list[PriceRow]:
    return [PriceRow(series, d, sp, "NA", value) for sp in range(1, 49)]


def test_full_window_available_true_when_every_day_is_a_complete_48_periods(tmp_path):
    conn = connect(tmp_path / "test.db")
    rows = []
    for d in fm.forecast_window_days(TODAY):
        rows += _full_day_rows("wind_forecast_14d", d, 100.0)
    # Day 1 comes from the short-term series, not the medium-term one.
    rows += _full_day_rows("wind_forecast", DAY1, 100.0)
    upsert_prices(conn, [r for r in rows if not (r.sd == DAY1 and r.series == "wind_forecast_14d")])

    assert fm.full_window_available(conn, fm.wind_forecast, TODAY) is True


def test_full_window_available_false_when_one_day_is_short(tmp_path):
    conn = connect(tmp_path / "test.db")
    rows = []
    for d in fm.forecast_window_days(TODAY):
        rows += _full_day_rows("wind_forecast_14d", d, 100.0)
    rows += _full_day_rows("wind_forecast", DAY1, 100.0)
    rows = [r for r in rows if not (r.sd == DAY1 and r.series == "wind_forecast_14d")]
    # Drop the last day down to a partial 26 periods, matching the real
    # gap seen live (the rolling window's far edge doesn't always land on
    # a full local day).
    rows = [r for r in rows if not (r.sd == DAY14 and r.sp > 26)]
    upsert_prices(conn, rows)

    assert fm.full_window_available(conn, fm.wind_forecast, TODAY) is False


def test_full_window_available_false_when_nothing_ingested(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert fm.full_window_available(conn, fm.wind_forecast, TODAY) is False
