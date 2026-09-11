import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest.eac import expand  # noqa: E402


def _record(**overrides):
    base = {
        "_id": 1177182,
        "unitResultID": "2832#||#12#||#PQR#||#201564",
        "serviceType": "Quick Reserve",
        "auctionProduct": "PQR",
        "technologyType": "Batteries",
        "auctionUnit": "DOLLB-1",
        "registeredAuctionParticipant": "Statkraft Markets GmbH",
        "executedQuantity": "39.0",
        "clearingPrice": "4.9",
        "deliveryStart": "2026-09-05T06:00:00",
        "deliveryEnd": "2026-09-05T06:30:00",
        "postCode": "SS11 8UA",
    }
    base.update(overrides)
    return base


def test_single_period_record_produces_one_row():
    rows = expand([_record()])
    assert len(rows) == 1
    r = rows[0]
    assert r.neso_id == 1177182
    assert r.sd == date(2026, 9, 5)
    assert r.sp == 13
    assert r.executed_quantity == 39.0
    assert r.clearing_price == 4.9
    assert r.participant == "Statkraft Markets GmbH"
    assert r.technology_type == "Batteries"


def test_four_hour_response_block_expands_to_eight_periods():
    # A live-sampled Response (DC/DM/DR) delivery block: 4 hours -> 8 half-hour periods.
    rows = expand([_record(
        serviceType="Response", auctionProduct="DCL",
        deliveryStart="2026-09-11T02:00:00", deliveryEnd="2026-09-11T06:00:00",
    )])
    assert len(rows) == 8
    sps = [r.sp for r in rows]
    assert sps == [5, 6, 7, 8, 9, 10, 11, 12]
    # every expanded row carries the same source id and the full field set
    assert all(r.neso_id == 1177182 for r in rows)
    assert all(r.service_type == "Response" for r in rows)
    assert all(r.clearing_price == 4.9 for r in rows)


def test_expansion_spanning_midnight_advances_settlement_date():
    rows = expand([_record(
        deliveryStart="2026-09-05T23:30:00", deliveryEnd="2026-09-06T00:30:00",
    )])
    assert len(rows) == 2
    assert (rows[0].sd, rows[0].sp) == (date(2026, 9, 5), 48)
    assert (rows[1].sd, rows[1].sp) == (date(2026, 9, 6), 1)


def test_null_quantity_and_price_become_none():
    rows = expand([_record(executedQuantity=None, clearingPrice="")])
    assert rows[0].executed_quantity is None
    assert rows[0].clearing_price is None


def test_zero_price_is_not_treated_as_null():
    rows = expand([_record(clearingPrice="0.0")])
    assert rows[0].clearing_price == 0.0


def test_missing_postcode_is_none():
    rows = expand([_record(postCode=None)])
    assert rows[0].post_code is None


def test_unique_key_differs_per_expanded_period():
    rows = expand([_record(
        deliveryStart="2026-09-11T02:00:00", deliveryEnd="2026-09-11T06:00:00",
    )])
    keys = {(r.neso_id, r.sd, r.sp) for r in rows}
    assert len(keys) == len(rows)  # no collisions -- each is a valid upsert PK
