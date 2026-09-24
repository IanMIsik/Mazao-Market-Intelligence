import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw.ingest.neso_demand_medium import _ctime_to_sp  # noqa: E402


def test_ctime_to_sp_matches_real_values_from_the_source():
    # Real CTIME values seen in a live fetch: "30" is the first
    # half-hour's own end (00:30 -> SP1), "2400" is the last (24:00,
    # i.e. midnight -> SP48).
    assert _ctime_to_sp("30") == 1
    assert _ctime_to_sp("100") == 2
    assert _ctime_to_sp("130") == 3
    assert _ctime_to_sp("230") == 5
    assert _ctime_to_sp("1200") == 24
    assert _ctime_to_sp("2330") == 47
    assert _ctime_to_sp("2400") == 48
