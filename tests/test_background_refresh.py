import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.storage import PriceRow, upsert_prices  # noqa: E402
from gbpw.storage import connect as gbpw_connect  # noqa: E402
from gbpw.web import background_refresh  # noqa: E402


def test_singleton_lock_second_acquire_fails_while_first_holds(tmp_path):
    lock_path = tmp_path / ".refresh.lock"
    first = background_refresh._try_acquire_singleton_lock(lock_path)
    assert first is not None

    second = background_refresh._try_acquire_singleton_lock(lock_path)
    assert second is None

    first.close()


def test_singleton_lock_released_on_close(tmp_path):
    lock_path = tmp_path / ".refresh.lock"
    first = background_refresh._try_acquire_singleton_lock(lock_path)
    first.close()

    # The OS releases the lock the instant the holding handle closes --
    # a second process (here, just a second call) must be able to win it
    # immediately, not be blocked by a stale marker left behind.
    second = background_refresh._try_acquire_singleton_lock(lock_path)
    assert second is not None
    second.close()


def test_start_background_refresh_skips_loop_when_lock_already_held(tmp_path, monkeypatch):
    db_path = tmp_path / "gbpw.db"
    holder = background_refresh._try_acquire_singleton_lock(tmp_path / ".refresh.lock")

    called = []
    monkeypatch.setattr(background_refresh, "_refresh_once", lambda p: called.append(p))

    stop = background_refresh.start_background_refresh(db_path, interval_seconds=10)
    time.sleep(0.05)  # a wrongly-started thread would have called _refresh_once by now
    stop.set()

    assert called == []
    holder.close()


def test_start_background_refresh_runs_loop_when_lock_free(tmp_path, monkeypatch):
    db_path = tmp_path / "gbpw.db"

    called = []
    monkeypatch.setattr(background_refresh, "_refresh_once", lambda p: called.append(p))

    stop = background_refresh.start_background_refresh(db_path, interval_seconds=10)
    time.sleep(0.05)
    stop.set()

    assert called == [db_path]


def test_refresh_once_ingests_the_whole_in_progress_week_not_just_today(tmp_path, monkeypatch):
    # Regression test for a real bug: this used to call
    # ingest_week_parallel(db_path, [today]) only, on the assumption that
    # once a day becomes "yesterday" it's already fully covered by GB
    # Power Weekly's own ingest -- true for *prior* weeks, never true for
    # the current week's own past days (which the day-picker on Live
    # Market can show, see routes_live.py). A day only ever got whatever
    # periods were captured while it was still "today"; a missed cycle
    # left a permanent gap nothing ever revisited. Confirmed live before
    # writing this fix.
    monkeypatch.setattr(background_refresh, "london_today", lambda: date(2026, 9, 24))  # a Thursday

    monkeypatch.setattr(background_refresh, "ingest_bmu_reference", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_eac_range_parallel", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_bm_cashflows_range_parallel", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_embedded_forecasts", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_wind_curtailment", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_fuelinst", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_carbon_intensity", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_imrp", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_forecast_medium_term", lambda conn, today: None)
    monkeypatch.setattr(background_refresh, "ingest_interconnector_scheduled", lambda conn, today: None)
    # A real (empty) connection, not a stub -- the wind-curtailment
    # backfill check below queries `prices` directly.
    monkeypatch.setattr(background_refresh, "connect", lambda db_path: gbpw_connect(db_path))

    captured = {}
    monkeypatch.setattr(background_refresh, "ingest_week_parallel", lambda db_path, dates: captured.setdefault("dates", dates))

    background_refresh._refresh_once(tmp_path / "gbpw.db")

    assert captured["dates"] == [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]


def test_refresh_once_only_backfills_wind_curtailment_for_incomplete_past_days(tmp_path, monkeypatch):
    # Regression test for a real bug found right after the fix above:
    # wind_curtailed_mw has the exact same "never revisited once it's
    # yesterday" problem, but ISPSTACK has no bulk endpoint (one call per
    # settlement period, see ingest_wind_curtailment()'s own docstring),
    # so this can't just be widened the same way without re-running the
    # whole week's worth of per-period calls every 5-minute cycle
    # forever. Confirmed here: a past day already at full 48/48 is left
    # alone, a past day still short of 48 gets backfilled, and today is
    # always re-ingested regardless (it's expected to be incomplete until
    # the day actually ends).
    today = date(2026, 9, 24)  # Thursday -- week is Mon 21..Thu 24
    monkeypatch.setattr(background_refresh, "london_today", lambda: today)

    monkeypatch.setattr(background_refresh, "ingest_bmu_reference", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_eac_range_parallel", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_bm_cashflows_range_parallel", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_week_parallel", lambda *a, **k: None)
    monkeypatch.setattr(background_refresh, "ingest_embedded_forecasts", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_interconnector_scheduled", lambda conn, today: None)
    monkeypatch.setattr(background_refresh, "ingest_fuelinst", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_carbon_intensity", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_imrp", lambda conn: None)
    monkeypatch.setattr(background_refresh, "ingest_forecast_medium_term", lambda conn, today: None)
    monkeypatch.setattr(background_refresh, "connect", lambda db_path: gbpw_connect(db_path))

    db_path = tmp_path / "gbpw.db"
    conn = gbpw_connect(db_path)
    # Mon 21 Sep: already complete (48/48) -- must NOT be re-fetched.
    upsert_prices(conn, [PriceRow("wind_curtailed_mw", date(2026, 9, 21), sp, "NA", 5.0) for sp in range(1, 49)])
    # Tue 22 Sep: only half-populated -- must be backfilled.
    upsert_prices(conn, [PriceRow("wind_curtailed_mw", date(2026, 9, 22), sp, "NA", 5.0) for sp in range(1, 25)])
    # Wed 23 Sep: nothing at all -- must be backfilled.
    conn.close()

    called = []
    monkeypatch.setattr(background_refresh, "ingest_wind_curtailment", lambda conn, d: called.append(d))

    background_refresh._refresh_once(db_path)

    assert date(2026, 9, 21) not in called  # already complete, left alone
    assert date(2026, 9, 22) in called      # partial, backfilled
    assert date(2026, 9, 23) in called      # empty, backfilled
    assert called.count(today) == 1         # today is always re-ingested, exactly once
