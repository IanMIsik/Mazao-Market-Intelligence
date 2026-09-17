import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import lccc  # noqa: E402


def _record(**overrides):
    base = {"IMRP_Date": "2016-06-30 00:00:00.0000000", "Settlement_Period": "1", "IMRP_Amount": "29.55"}
    base.update(overrides)
    return base


def test_parse_splits_one_hourly_record_into_two_half_hourly_rows():
    rows = lccc.parse([_record()])

    assert len(rows) == 2
    assert [r.sp for r in rows] == [1, 2]
    assert all(r.sd == date(2016, 6, 30) for r in rows)
    assert all(r.value == 29.55 for r in rows)
    assert all(r.series == "imrp" for r in rows)
    assert all(r.run == "NA" for r in rows)


def test_parse_first_hour_maps_to_sp_1_and_2():
    rows = lccc.parse([_record(Settlement_Period="1")])
    assert [r.sp for r in rows] == [1, 2]


def test_parse_last_hour_maps_to_sp_47_and_48():
    rows = lccc.parse([_record(Settlement_Period="24")])
    assert [r.sp for r in rows] == [47, 48]


def test_parse_middle_hour():
    # hour 13 (1pm) -> sp (13-1)*2+1=25, 26
    rows = lccc.parse([_record(Settlement_Period="13")])
    assert [r.sp for r in rows] == [25, 26]


def test_parse_multiple_records_accumulate():
    records = [
        _record(Settlement_Period="1", IMRP_Amount="10.0"),
        _record(Settlement_Period="2", IMRP_Amount="20.0"),
        _record(IMRP_Date="2016-07-01 00:00:00.0000000", Settlement_Period="1", IMRP_Amount="30.0"),
    ]
    rows = lccc.parse(records)

    assert len(rows) == 6
    by_date = {}
    for r in rows:
        by_date.setdefault(r.sd, []).append((r.sp, r.value))
    assert by_date[date(2016, 6, 30)] == [(1, 10.0), (2, 10.0), (3, 20.0), (4, 20.0)]
    assert by_date[date(2016, 7, 1)] == [(1, 30.0), (2, 30.0)]


def test_fetch_all_paginates_until_total_reached(monkeypatch):
    pages = [
        {"records": [{"id": i} for i in range(3)], "total": 7},
        {"records": [{"id": i} for i in range(3, 6)], "total": 7},
        {"records": [{"id": 6}], "total": 7},
    ]
    calls: list[dict] = []

    def fake_get(params):
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(lccc, "_get", fake_get)
    records = lccc._fetch_all()

    assert [r["id"] for r in records] == [0, 1, 2, 3, 4, 5, 6]
    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == 3
    assert calls[2]["offset"] == 6


def test_fetch_all_stops_on_empty_page_even_if_total_not_reached(monkeypatch):
    pages = [
        {"records": [{"id": 0}], "total": 5},
        {"records": [], "total": 5},
    ]
    calls: list[dict] = []

    def fake_get(params):
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(lccc, "_get", fake_get)
    records = lccc._fetch_all()

    assert len(records) == 1


def test_fetch_imrp_returns_rows_and_note(monkeypatch):
    monkeypatch.setattr(lccc, "_fetch_all", lambda: [_record(Settlement_Period="1"), _record(Settlement_Period="2")])
    rows, note = lccc.fetch_imrp()

    assert len(rows) == 4
    assert "2 source rows" in note
    assert "4 settlement-period rows" in note
