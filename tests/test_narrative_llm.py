import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbpw import narrative_llm, narrative_rules  # noqa: E402

# Mirrors the sketch's own sample figures, so the test data reads naturally
# against the numbers used throughout this project's docs and the reference page.
FACTS = {
    "week_ending": "2026-08-30",
    "week_start": "2026-08-24",
    "kpi": {
        "avg_day_ahead": 75.86,
        "avg_day_ahead_pct_change": 10.1,
        "best_spread_week": 94.71,
        "best_spread_week_day": "Wed 26 Aug",
        "spread_30d_median": 62.37,
        "wind_share_pct": 32,
        "wind_share_pct_change_pts": -7,
        "highest_imbalance_value": 193.0,
        "highest_imbalance_day_label": "Thu 27 Aug",
        "highest_imbalance_sp": 38,
    },
    "days": [
        {
            "date": "2026-08-24", "label": "Mon 24 Aug", "short_label": "Mon 24", "dow_letter": "M",
            "periods": 48, "day_ahead_avg": 68.80, "day_ahead_peak": 92.71, "day_ahead_trough": 36.08,
            "best_spread": 53.27, "wind_share_pct": 41.0, "wind_avg_gw": 10.2,
            "demand_peak_gw": 28.4, "periods_above_100": 0,
        },
    ],
    "totals": {"periods_above_100_week": 29},
    "drivers": {
        "wind": {"low_gw": 4.8, "low_day": "Wed 26 Aug", "high_gw": 14.8, "high_day": "Sat 29 Aug"},
        "demand": {"peak_gw": 31.2, "peak_day": "Fri 28 Aug"},
        "periods_above_100": {"high": 29, "high_day": "Wed 26 Aug", "low": 0, "low_day": "Mon 24 Aug"},
    },
    # Raw per-period series -- must never reach the model, and a value that
    # appears *only* here (never in kpi/days/drivers) must NOT validate,
    # proving the check is against the reduced payload, not the full facts.
    "half_hourly": {
        "day_ahead": [{"sd": "2026-08-24", "sp": 1, "value": 68.8, "day_label": "Mon 24 Aug"}],
        "imbalance": [{"sd": "2026-08-24", "sp": 1, "value": 71234.0, "day_label": "Mon 24 Aug"}],
    },
    "heatmap": [{"sd": "2026-08-24", "sp": 1, "day_label": "Mon", "delta": -17.0}],
    "run_basis": "latest available run per period",
}


def _payload_and_allowed():
    payload = narrative_llm._narrative_payload(FACTS)
    payload_json = json.dumps(payload, sort_keys=True)
    allowed = set(narrative_llm._extract_numbers(payload_json))
    return payload, allowed


def test_narrative_payload_excludes_raw_series():
    payload, allowed = _payload_and_allowed()
    assert "half_hourly" not in payload
    assert "heatmap" not in payload
    # 71234.0 only exists inside half_hourly; 17 only exists inside heatmap's
    # delta (as -17.0). Neither should be reachable via the reduced payload.
    assert 71234.0 not in allowed
    assert 17.0 not in allowed
    assert -17.0 not in allowed


def test_validate_rejects_number_absent_from_facts():
    _, allowed = _payload_and_allowed()
    parsed = {
        "headline": "Prices settled around £75.86/MWh this week, a typical range.",
        "byline": "Battery value held near £94.71/MWh, roughly in line with recent weeks.",
        "drivers": [
            "Wind output covered a wide band across the week.",
            "Demand stayed within its usual range for the period.",
            # 17 is not present anywhere in the reduced payload (it's only in
            # the withheld heatmap), so this must be rejected.
            "Prices cleared above £100 on 17 separate settlement periods.",
        ],
    }
    with pytest.raises(narrative_llm.ValidationError):
        narrative_llm._validate(parsed, allowed)


def test_currency_figure_matches_after_normalisation():
    _, allowed = _payload_and_allowed()
    nums = narrative_llm._extract_numbers("Day-ahead averaged £75.86/MWh across the week.")
    assert 75.86 in nums
    assert 75.86 in allowed  # confirms the match actually lands against a real fact


def test_percent_figure_matches_after_normalisation():
    _, allowed = _payload_and_allowed()
    nums = narrative_llm._extract_numbers("That is up 10.1% on the prior week.")
    assert 10.1 in nums
    assert 10.1 in allowed


def test_valid_output_passes_and_is_returned_unchanged():
    _, allowed = _payload_and_allowed()
    parsed = {
        "headline": "Average day-ahead reached £75.86/MWh, up 10.1% on the prior week.",
        "byline": "Best 1-hour spread was £94.71/MWh, on Wed 26 Aug. Wind share of generation was 32%.",
        "drivers": [
            "Wind output ranged from 4.8 GW on Wed 26 Aug to 14.8 GW on Sat 29 Aug.",
            "Peak demand for the week was 31.2 GW, on Fri 28 Aug.",
            "29 settlement periods cleared above £100 on Wed 26 Aug, against 0 on Mon 24 Aug.",
        ],
    }
    result = narrative_llm._validate(parsed, allowed)
    assert result == parsed


def _mock_client(reply_after_brace: str) -> MagicMock:
    """Build a mock anthropic.Anthropic() whose messages.create() returns the
    given text as the continuation after our "{" prefill, exactly like the
    real streaming-free API would.
    """
    client = MagicMock()
    content_block = MagicMock()
    content_block.text = reply_after_brace
    response = MagicMock()
    response.content = [content_block]
    client.messages.create.return_value = response
    return client


def test_generate_returns_valid_result_on_first_attempt():
    valid = {
        "headline": "Average day-ahead reached £75.86/MWh, up 10.1% on the prior week.",
        "byline": "Best 1-hour spread was £94.71/MWh, on Wed 26 Aug. Wind share of generation was 32%.",
        "drivers": [
            "Wind output ranged from 4.8 GW on Wed 26 Aug to 14.8 GW on Sat 29 Aug.",
            "Peak demand for the week was 31.2 GW, on Fri 28 Aug.",
            "29 settlement periods cleared above £100 on Wed 26 Aug, against 0 on Mon 24 Aug.",
        ],
    }
    reply_after_brace = json.dumps(valid)[1:]  # strip the leading "{" the prefill already supplied
    client = _mock_client(reply_after_brace)

    with patch.object(narrative_llm.anthropic, "Anthropic", return_value=client), \
         patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        result = narrative_llm.generate(FACTS)

    assert result == valid
    assert client.messages.create.call_count == 1  # no retry needed


def test_generate_falls_through_to_none_then_rules_cleanly():
    # Every attempt returns a hallucinated number (17), so validation fails
    # both times and generate() must return None without raising.
    bad = {
        "headline": "Prices cleared above £100 on 17 separate periods this week.",
        "byline": "Best 1-hour spread was £94.71/MWh, on Wed 26 Aug. Wind share of generation was 32%.",
        "drivers": [
            "Wind output ranged from 4.8 GW on Wed 26 Aug to 14.8 GW on Sat 29 Aug.",
            "Peak demand for the week was 31.2 GW, on Fri 28 Aug.",
            "29 settlement periods cleared above £100 on Wed 26 Aug, against 0 on Mon 24 Aug.",
        ],
    }
    reply_after_brace = json.dumps(bad)[1:]
    client = _mock_client(reply_after_brace)

    with patch.object(narrative_llm.anthropic, "Anthropic", return_value=client), \
         patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        result = narrative_llm.generate(FACTS)

    assert result is None
    assert client.messages.create.call_count == 2  # original attempt + one retry

    # The caller's fallback contract: None -> rules, and rules always succeeds.
    fallback = result or narrative_rules.generate(FACTS)
    assert fallback is not None
    assert set(fallback.keys()) == {"headline", "byline", "drivers"}
    assert len(fallback["drivers"]) == 3


def test_generate_returns_none_without_api_key():
    with patch.dict("os.environ", {}, clear=True):
        assert narrative_llm.generate(FACTS) is None
