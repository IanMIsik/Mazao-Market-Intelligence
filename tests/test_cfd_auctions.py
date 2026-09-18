import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest import cfd_auctions  # noqa: E402


def _record(**overrides):
    base = {
        "_id": 604, "Auction": "AR6", "Version": "Final", "Publication_Date": "2024-09-03 00:00:00.0000000",
        "Project_Name": "Charleston", "Developer": "SONNEDIX WESTON LTD", "Technology_Type": "Solar PV (> 5MW)",
        "Capacity_MW": "20.00", "Strike_Price_GBP_Per_MWh": "50.070", "Delivery_Year": "2026",
        "Homes_Powered": "", "Region": "Scotland",
    }
    base.update(overrides)
    return base


def test_parse_maps_real_ar6_record():
    rows = cfd_auctions.parse([_record()])

    assert len(rows) == 1
    row = rows[0]
    assert row.lccc_id == 604
    assert row.auction == "AR6"
    assert row.project_name == "Charleston"
    assert row.developer == "SONNEDIX WESTON LTD"
    assert row.technology_type == "Solar PV (> 5MW)"
    assert row.capacity_mw == 20.0
    assert row.strike_price_gbp_mwh == 50.070
    assert row.price_base_year == 2012  # AR6 -- see PRICE_BASE_YEAR
    assert row.delivery_year == "2026"
    assert row.region == "Scotland"
    assert row.publication_date == "2024-09-03"


def test_parse_ar7_uses_2024_base_year():
    rows = cfd_auctions.parse([_record(Auction="AR7", _id=999)])
    assert rows[0].price_base_year == 2024


def test_parse_skips_auction_not_in_price_base_year():
    # A future round (e.g. AR8) whose price basis hasn't been confirmed yet
    # must not be stored with a guessed year -- it's simply skipped.
    rows = cfd_auctions.parse([_record(Auction="AR8", _id=1000)])
    assert rows == []


def test_parse_handles_missing_optional_fields():
    rows = cfd_auctions.parse([_record(Capacity_MW="", Developer="", Homes_Powered="")])
    row = rows[0]
    assert row.capacity_mw is None
    assert row.developer is None


def test_parse_multiple_records():
    records = [_record(_id=1, Auction="AR1"), _record(_id=2, Auction="AR7"), _record(_id=3, Auction="AR8")]
    rows = cfd_auctions.parse(records)
    # AR8 skipped -- only 2 of the 3 stored
    assert {r.lccc_id for r in rows} == {1, 2}


def test_fetch_all_paginates(monkeypatch):
    pages = [
        {"records": [{"id": i} for i in range(3)], "total": 5},
        {"records": [{"id": i} for i in range(3, 5)], "total": 5},
    ]
    calls = []

    def fake_get(params):
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(cfd_auctions, "_get", fake_get)
    records = cfd_auctions._fetch_all()

    assert [r["id"] for r in records] == [0, 1, 2, 3, 4]
    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == 3


def test_fetch_cfd_auction_outcomes_reports_skipped_rounds(monkeypatch):
    monkeypatch.setattr(
        cfd_auctions, "_fetch_all",
        lambda: [_record(_id=1, Auction="AR7"), _record(_id=2, Auction="AR8")],
    )
    rows, note = cfd_auctions.fetch_cfd_auction_outcomes()

    assert len(rows) == 1
    assert "2 source rows" in note
    assert "1 stored" in note
    assert "1 skipped" in note
