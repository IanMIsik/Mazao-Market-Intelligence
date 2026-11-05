import sys
import threading
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import entsoe_flows  # noqa: E402

D = date(2026, 11, 4)


def _hourly_series(value: float) -> pd.Series:
    idx = pd.date_range(pd.Timestamp(D, tz="Europe/Brussels"), periods=24, freq="h")
    return pd.Series(value, index=idx)


def test_the_client_has_a_real_timeout_not_entsoe_pys_wait_forever_default(monkeypatch):
    # Regression: entsoe-py defaults to timeout=None. A stalled ENTSO-E server froze the whole
    # background refresh (and so Live Market) for 30+ hours on 2026-11-04.
    monkeypatch.setenv("ENTSOE_KEY", "test-key")
    client = entsoe_flows._client()
    assert client.timeout == entsoe_flows.ENTSOE_TIMEOUT_SECONDS
    assert client.retry_count == entsoe_flows.ENTSOE_RETRIES


def test_one_hung_link_cannot_hang_the_whole_fetch(monkeypatch):
    monkeypatch.setenv("ENTSOE_KEY", "test-key")
    monkeypatch.setattr(entsoe_flows, "ENTSOE_DEADLINE_SECONDS", 0.5)
    hung = next(iter(entsoe_flows.PAIRS))
    release = threading.Event()

    def fake_fetch(client, pair, start, end):
        if pair == hung:
            release.wait(10)  # a server that never answers
            return None
        return _hourly_series(100.0)

    monkeypatch.setattr(entsoe_flows, "_fetch_pair_net", fake_fetch)

    t0 = time.monotonic()
    rows, note = entsoe_flows.fetch_continental_scheduled(D)
    elapsed = time.monotonic() - t0
    release.set()

    assert elapsed < 3  # gave up at the deadline instead of waiting for the hung link
    assert f"failed: ['{entsoe_flows.PAIRS[hung]}']" in note
    assert rows  # the six healthy links still delivered
