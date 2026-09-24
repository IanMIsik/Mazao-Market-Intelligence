import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
