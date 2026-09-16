import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gbpw.metrics import build_week  # noqa: E402
from gbpw.settlement import week_dates  # noqa: E402
from gbpw.storage import EacRow, PriceRow, connect, upsert_eac_results, upsert_prices, upsert_report  # noqa: E402
from gbpw.web.app import create_app  # noqa: E402

WEEK_ENDING = date(2026, 8, 30)
EAC_START = date(2026, 9, 8)
EAC_END = date(2026, 9, 10)


def _client(db_path):
    return TestClient(create_app(db_path=db_path))


def _seed_gbpw_report(conn, week_ending=WEEK_ENDING, headline="Test week headline."):
    """Seeds a real week of Elexon-style data and builds real facts via
    build_week(), rather than hand-writing a facts dict -- avoids drifting
    out of sync with metrics.py's actual output shape.
    """
    dates = week_dates(week_ending)
    for d in dates:
        rows = []
        for sp in range(1, 49):
            rows.append(PriceRow("day_ahead", d, sp, "NA", 50.0))
            rows.append(PriceRow("imbalance", d, sp, "latest", 55.0))
            rows.append(PriceRow("wind", d, sp, "NA", 3000.0))
            rows.append(PriceRow("total_generation", d, sp, "NA", 10000.0))
            rows.append(PriceRow("demand", d, sp, "NA", 25000.0))
        upsert_prices(conn, rows)

    facts = build_week(conn, week_ending)
    narrative = {"headline": headline, "byline": "Test byline.", "drivers": ["a", "b", "c"]}
    upsert_report(
        conn,
        week_ending=week_ending,
        facts_json=json.dumps(facts),
        narrative=json.dumps(narrative),
        run_basis=facts["run_basis"],
        built_at=datetime.now(timezone.utc),
    )
    return facts, narrative


def _eac_row(**overrides):
    base = dict(
        neso_id=1, unit_result_id="u1", service_type="Response", auction_product="DCL",
        technology_type="Batteries", auction_unit="AUNIT01", participant="Alpha Energy",
        executed_quantity=10.0, clearing_price=5.0,
        delivery_start="2026-09-08T00:00:00", delivery_end="2026-09-08T00:30:00",
        sd=EAC_START, sp=1, post_code=None,
    )
    base.update(overrides)
    return EacRow(**base)


def test_bess_page_loads_with_seeded_data(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_eac_results(conn, [_eac_row()])
    client = _client(tmp_path / "test.db")
    r = client.get("/bess")
    assert r.status_code == 200
    assert "BESS Analytics" in r.text
    assert "Alpha Energy" not in r.text  # not a participant name shown on the aggregate page by default


def test_bess_page_empty_state_when_no_eac_data(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/bess")
    assert r.status_code == 200
    assert "No EAC data ingested" in r.text
    assert "No Balancing Mechanism cashflow matched" in r.text


def test_bess_page_today_card_empty_state_when_nothing_cleared_today(tmp_path):
    # _eac_row() defaults to EAC_START (a fixed historical date), so a
    # normal seeded page has no data for "today" -- no special setup needed.
    conn = connect(tmp_path / "test.db")
    upsert_eac_results(conn, [_eac_row()])
    client = _client(tmp_path / "test.db")
    r = client.get("/bess")
    assert r.status_code == 200
    assert "Today's auctions" in r.text
    assert "No EAC auctions have cleared yet today." in r.text


def test_bess_page_today_card_shows_real_data_and_is_independent_of_window(tmp_path):
    today = date.today()
    conn = connect(tmp_path / "test.db")
    upsert_eac_results(conn, [
        _eac_row(neso_id=1, auction_unit="AUNIT01", participant="Alpha Energy", sd=today,
                  delivery_start=f"{today.isoformat()}T00:00:00", delivery_end=f"{today.isoformat()}T00:30:00"),
    ])
    client = _client(tmp_path / "test.db")

    for window in (7, 30, 90):
        r = client.get(f"/bess?window={window}")
        assert r.status_code == 200
        assert "Today's auctions" in r.text
        assert "No EAC auctions have cleared yet today." not in r.text
        assert "1 participant(s) have cleared at least one EAC service so far today." in r.text


def test_gbpw_weekly_page_renders_from_stored_report(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed_gbpw_report(conn)
    client = _client(tmp_path / "test.db")
    r = client.get(f"/gbpw/{WEEK_ENDING.isoformat()}")
    assert r.status_code == 200
    assert "Test week headline." in r.text


def test_gbpw_single_week_has_no_picker(tmp_path):
    # A dropdown with one option is noise, not a feature -- only show it
    # once there's actually somewhere else to go.
    conn = connect(tmp_path / "test.db")
    _seed_gbpw_report(conn)
    client = _client(tmp_path / "test.db")
    r = client.get(f"/gbpw/{WEEK_ENDING.isoformat()}")
    assert "webnav-week-select" not in r.text


def test_gbpw_multiple_weeks_shows_picker_with_all_weeks_selected_current(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed_gbpw_report(conn)
    prior_week = date(2026, 8, 23)
    _seed_gbpw_report(conn, week_ending=prior_week, headline="Prior week headline.")
    client = _client(tmp_path / "test.db")

    r = client.get(f"/gbpw/{WEEK_ENDING.isoformat()}")
    assert "webnav-week-select" in r.text
    assert f'value="{WEEK_ENDING.isoformat()}" selected' in r.text
    assert f'value="{prior_week.isoformat()}">' in r.text  # listed, not selected

    r2 = client.get(f"/gbpw/{prior_week.isoformat()}")
    assert f'value="{prior_week.isoformat()}" selected' in r2.text


def test_gbpw_latest_redirects_to_stored_week(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed_gbpw_report(conn)
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].endswith(WEEK_ENDING.isoformat())


def test_gbpw_404_for_unknown_week(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/2099-01-01")
    assert r.status_code == 404


def test_gbpw_422_for_malformed_week_ending(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/not-a-date")
    assert r.status_code == 422


def test_gbpw_new_form_renders(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/new")
    assert r.status_code == 200
    assert "Build report" in r.text


def test_gbpw_new_form_prefills_week_ending_from_query(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/new?week_ending=2026-08-23")
    assert 'value="2026-08-23"' in r.text


def test_gbpw_build_rejects_malformed_date_without_ingesting(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/build?week_ending=not-a-date")
    assert r.status_code == 200  # re-renders the form, doesn't redirect
    assert "isn&#39;t a valid date" in r.text or "isn't a valid date" in r.text


def test_gbpw_build_rejects_non_sunday(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/build?week_ending=2026-08-25")  # a Tuesday
    assert r.status_code == 200
    assert "Sunday" in r.text


def test_gbpw_build_rejects_future_week(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/gbpw/build?week_ending=2099-01-04")  # a Sunday, far in the future
    assert r.status_code == 200
    assert "hasn&#39;t finished yet" in r.text or "hasn't finished yet" in r.text


def test_participant_search_api_returns_matching_json(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_eac_results(conn, [
        _eac_row(neso_id=1, participant="Alpha Energy"),
        _eac_row(neso_id=2, participant="Beta Storage", auction_unit="AUNIT02"),
    ])
    client = _client(tmp_path / "test.db")
    r = client.get("/api/eac/participants/search", params={"q": "alpha"})
    assert r.status_code == 200
    assert r.json() == [{"participant": "Alpha Energy"}]


def test_participant_detail_api_handles_multiple_p_params(tmp_path):
    conn = connect(tmp_path / "test.db")
    upsert_eac_results(conn, [
        _eac_row(neso_id=1, participant="Alpha Energy", auction_unit="AUNIT01"),
        _eac_row(neso_id=2, participant="Beta Storage", auction_unit="AUNIT02"),
    ])
    client = _client(tmp_path / "test.db")
    r = client.get("/api/eac/participants/detail", params=[("p", "Alpha Energy"), ("p", "Beta Storage"), ("window", 7)])
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"Alpha Energy", "Beta Storage"}
    assert data["Alpha Energy"]["eac"]["total_cleared_mw"] == 10.0


def test_index_redirects_to_bess(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    client = _client(tmp_path / "test.db")
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/bess"
