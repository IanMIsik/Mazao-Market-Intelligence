import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import eac_metrics  # noqa: E402
from gbpw.storage import EacRow, connect, upsert_eac_results  # noqa: E402

START = date(2026, 9, 8)
END = date(2026, 9, 10)


def _row(**overrides):
    base = dict(
        neso_id=1,
        unit_result_id="u1",
        service_type="Response",
        auction_product="DCL",
        technology_type="Batteries",
        auction_unit="AUNIT01",
        participant="Alpha Energy",
        executed_quantity=10.0,
        clearing_price=5.0,
        delivery_start="2026-09-08T00:00:00",
        delivery_end="2026-09-08T00:30:00",
        sd=START,
        sp=1,
        post_code=None,
    )
    base.update(overrides)
    return EacRow(**base)


def _seed(conn):
    rows = [
        # Alpha Energy: Response DCL(10@5) + DCH(6@8) + Quick Reserve(4@3) -> total 20
        _row(neso_id=1, participant="Alpha Energy", auction_unit="AUNIT01",
             service_type="Response", auction_product="DCL", executed_quantity=10.0, clearing_price=5.0),
        _row(neso_id=2, participant="Alpha Energy", auction_unit="AUNIT01",
             service_type="Response", auction_product="DCH", executed_quantity=6.0, clearing_price=8.0),
        _row(neso_id=3, participant="Alpha Energy", auction_unit="AUNIT01",
             service_type="Quick Reserve", auction_product="PQR", executed_quantity=4.0, clearing_price=3.0),
        # Beta Storage: Response DRL(30@4) + DRH(20@6) -> total 50
        _row(neso_id=4, participant="Beta Storage", auction_unit="AUNIT02",
             service_type="Response", auction_product="DRL", executed_quantity=30.0, clearing_price=4.0),
        _row(neso_id=5, participant="Beta Storage", auction_unit="AUNIT02", sd=END, sp=1,
             delivery_start="2026-09-10T00:00:00", delivery_end="2026-09-10T00:30:00",
             service_type="Response", auction_product="DRH", executed_quantity=20.0, clearing_price=6.0),
        # Gamma Power: Slow Reserve(5@2) -> total 5
        _row(neso_id=6, participant="Gamma Power", auction_unit="AUNIT03",
             service_type="Slow Reserve", auction_product="PSR", executed_quantity=5.0, clearing_price=2.0),
        # Delta Wind: not a battery -- must be excluded from Batteries-scoped queries
        _row(neso_id=7, participant="Delta Wind", auction_unit="AUNIT04", technology_type="Wind",
             service_type="Response", auction_product="DCL", executed_quantity=100.0, clearing_price=1.0),
    ]
    upsert_eac_results(conn, rows)


def test_market_summary_totals_by_service_type(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    summary = eac_metrics.market_summary(conn, START, END)
    by_type = {r["service_type"]: r for r in summary["by_service_type"]}
    assert by_type["Response"]["cleared_mw"] == 66.0
    assert by_type["Response"]["participants"] == 2
    assert by_type["Quick Reserve"]["cleared_mw"] == 4.0
    assert by_type["Slow Reserve"]["cleared_mw"] == 5.0
    assert "Delta Wind" not in [p for r in summary["by_service_type"] for p in [r]]  # scoped out via technology
    assert summary["participants_active"] == 3


def test_market_summary_response_band_split(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    summary = eac_metrics.market_summary(conn, START, END)
    by_band = {r["band"]: r["cleared_mw"] for r in summary["response_by_band"]}
    assert by_band["Low"] == 40.0   # DCL(10) + DRL(30)
    assert by_band["High"] == 26.0  # DCH(6) + DRH(20)


def test_distribution_bucket_counts(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    dist = eac_metrics.distribution(conn, START, END, n_buckets=2)
    assert dist["participant_count"] == 3
    assert sum(b["count"] for b in dist["buckets"]) == 3
    # totals per participant: Alpha 20, Beta 50, Gamma 5; width = 50/2 = 25
    assert dist["buckets"][0]["count"] == 2  # Alpha(20), Gamma(5)
    assert dist["buckets"][1]["count"] == 1  # Beta(50)


def test_leaderboard_ordering_and_volume_weighted_price(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    board = eac_metrics.leaderboard(conn, START, END, top_n=8)
    assert [r["participant"] for r in board] == ["Beta Storage", "Alpha Energy", "Gamma Power"]
    alpha = next(r for r in board if r["participant"] == "Alpha Energy")
    # (10*5 + 6*8 + 4*3) / 20 = 110/20 = 5.5 -- not a flat mean of (5,8,3)=5.33
    assert alpha["avg_clearing_price"] == 5.5


def test_search_participants_case_insensitive_substring_scoped(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    assert eac_metrics.search_participants(conn, "energy") == ["Alpha Energy"]
    assert eac_metrics.search_participants(conn, "STORAGE") == ["Beta Storage"]
    # Delta Wind matches the substring but is scoped out by technology_type
    assert eac_metrics.search_participants(conn, "delta") == []
    assert eac_metrics.search_participants(conn, "delta", technology_type=None) == ["Delta Wind"]


def test_participant_detail_breakdown(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    detail = eac_metrics.participant_detail(conn, ["Alpha Energy", "Beta Storage"], START, END)
    assert set(detail.keys()) == {"Alpha Energy", "Beta Storage"}
    assert detail["Alpha Energy"]["total_cleared_mw"] == 20.0
    assert detail["Alpha Energy"]["auction_units"] == ["AUNIT01"]
    assert len(detail["Alpha Energy"]["by_service_type"]) == 3  # DCL, DCH, Quick Reserve rows


def test_empty_range_returns_zeros_not_error(tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed(conn)
    future_start, future_end = date(2030, 1, 1), date(2030, 1, 7)
    summary = eac_metrics.market_summary(conn, future_start, future_end)
    assert summary["by_service_type"] == []
    assert summary["participants_active"] == 0
    assert eac_metrics.leaderboard(conn, future_start, future_end) == []
    assert eac_metrics.distribution(conn, future_start, future_end) == {"buckets": [], "participant_count": 0}
    assert eac_metrics.participant_detail(conn, ["Alpha Energy"], future_start, future_end) == {
        "Alpha Energy": {"by_service_type": [], "total_cleared_mw": 0.0, "auction_units": []}
    }


def test_latest_available_date(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert eac_metrics.latest_available_date(conn) is None
    _seed(conn)
    assert eac_metrics.latest_available_date(conn) == END
    assert eac_metrics.latest_available_date(conn, technology_type="Wind") == START


def test_earliest_available_date(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert eac_metrics.earliest_available_date(conn) is None
    _seed(conn)
    assert eac_metrics.earliest_available_date(conn) == START
    assert eac_metrics.earliest_available_date(conn, technology_type="Wind") == START
