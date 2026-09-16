"""
The *_parallel ingest functions (ingest_week_parallel, ingest_eac_range_parallel,
ingest_bm_cashflows_range_parallel) are new orchestration logic -- each worker
thread opens its own sqlite3 connection and writes independently -- which is
exactly the kind of thing that's easy to get subtly wrong (a shared connection
used across threads, a worker's writes silently lost, one bad date dropping
others). Unlike the plain HTTP-fetching functions elsewhere in ingest/ (this
codebase's convention is not to unit-test those -- see ingest/elexon.py etc.,
verified live instead), this threading mechanics IS worth a real test: real
sqlite file on disk, real ThreadPoolExecutor, network calls monkeypatched to
return canned data instantly so the test is fast and deterministic.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import ingest  # noqa: E402
from gbpw.ingest import eac as eac_module  # noqa: E402
from gbpw.ingest import elexon as elexon_module  # noqa: E402
from gbpw.ingest import elexon_bm as elexon_bm_module  # noqa: E402
from gbpw.storage import EacRow, connect  # noqa: E402


def _fake_day_ahead(d):
    return [], "ok"  # empty rows are fine -- we're testing fan-out/fetch_log, not price values


def _fake_imbalance(d):
    return [], "ok"


def _fake_generation(d):
    return [], [], "ok"


def _fake_demand(d):
    return [], "ok"


def test_ingest_week_parallel_processes_every_date(tmp_path, monkeypatch):
    monkeypatch.setattr(elexon_module, "fetch_day_ahead", _fake_day_ahead)
    monkeypatch.setattr(elexon_module, "fetch_imbalance", _fake_imbalance)
    monkeypatch.setattr(elexon_module, "fetch_generation", _fake_generation)
    monkeypatch.setattr(elexon_module, "fetch_demand", _fake_demand)

    db_path = tmp_path / "test.db"
    dates = [date(2026, 8, 1) + __import__("datetime").timedelta(days=i) for i in range(14)]
    ingest.ingest_week_parallel(db_path, dates, max_workers=4)

    conn = connect(db_path)
    logged_dates = {
        row[0] for row in conn.execute("SELECT DISTINCT sd FROM fetch_log WHERE series='day_ahead'").fetchall()
    }
    assert logged_dates == {d.isoformat() for d in dates}


def test_ingest_eac_range_parallel_writes_every_chunk(tmp_path, monkeypatch):
    def _fake_fetch_range(start, end, technology_type=None):
        return [
            EacRow(
                neso_id=hash((start, end)) % 1_000_000, unit_result_id="u1", service_type="Response",
                auction_product="DCL", technology_type="Batteries", auction_unit=f"UNIT-{start.isoformat()}",
                participant="Test Co", executed_quantity=10.0, clearing_price=5.0,
                delivery_start=f"{start.isoformat()}T00:00:00", delivery_end=f"{start.isoformat()}T00:30:00",
                sd=start, sp=1, post_code=None,
            )
        ]

    monkeypatch.setattr(eac_module, "fetch_range", _fake_fetch_range)

    db_path = tmp_path / "test.db"
    # 3 EAC_CHUNK_DAYS(7)-sized chunks' worth of range
    ingest.ingest_eac_range_parallel(db_path, date(2026, 1, 1), date(2026, 1, 21), max_workers=4)

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(DISTINCT auction_unit) FROM eac_results").fetchone()[0]
    assert count == 3  # one distinct auction_unit per chunk, proves all 3 chunks landed


def test_ingest_bm_cashflows_range_parallel_writes_every_date(tmp_path, monkeypatch):
    def _fake_fetch_cashflows(d, bid_offer):
        return [{
            "settlementDate": d.isoformat(), "settlementPeriod": 1,
            "nationalGridBmUnit": f"UNIT-{d.isoformat()}-{bid_offer}", "totalCashflow": 1.0,
        }]

    monkeypatch.setattr(elexon_bm_module, "fetch_cashflows", _fake_fetch_cashflows)

    db_path = tmp_path / "test.db"
    dates = [date(2026, 8, 1) + __import__("datetime").timedelta(days=i) for i in range(10)]
    ingest.ingest_bm_cashflows_range_parallel(db_path, dates[0], dates[-1], max_workers=4)

    conn = connect(db_path)
    logged_dates = {
        row[0] for row in conn.execute("SELECT DISTINCT sd FROM fetch_log WHERE series='bm_cashflow'").fetchall()
    }
    assert logged_dates == {d.isoformat() for d in dates}
    # both bid and offer rows landed for every date, not just one side
    row_count = conn.execute("SELECT COUNT(*) FROM bm_cashflows").fetchone()[0]
    assert row_count == len(dates) * 2


def test_ingest_bm_cashflows_range_parallel_one_bad_date_does_not_drop_others(tmp_path, monkeypatch):
    def _flaky_fetch_cashflows(d, bid_offer):
        if d == date(2026, 8, 3):
            raise RuntimeError("simulated Elexon error")
        return [{
            "settlementDate": d.isoformat(), "settlementPeriod": 1,
            "nationalGridBmUnit": f"UNIT-{d.isoformat()}", "totalCashflow": 1.0,
        }]

    monkeypatch.setattr(elexon_bm_module, "fetch_cashflows", _flaky_fetch_cashflows)

    db_path = tmp_path / "test.db"
    ingest.ingest_bm_cashflows_range_parallel(db_path, date(2026, 8, 1), date(2026, 8, 5), max_workers=4)

    conn = connect(db_path)
    ok_flags = dict(conn.execute("SELECT sd, ok FROM fetch_log WHERE series='bm_cashflow'").fetchall())
    assert ok_flags["2026-08-03"] == 0  # the bad date failed...
    assert ok_flags["2026-08-01"] == 1  # ...but every other date still succeeded
    assert ok_flags["2026-08-05"] == 1
