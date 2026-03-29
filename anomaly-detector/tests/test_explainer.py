import time
import pytest
from unittest.mock import patch, MagicMock

from src.explainer import (
    explain_anomaly,
    _fallback_explanation,
    _check_rate_limit,
    _call_timestamps,
    MAX_CLAUDE_CALLS_PER_HOUR,
)

# --- Fixtures ---

METRICS = {
    "error_rate": 0.9,
    "request_rate": 0.1,
    "p95_latency": 5.0,
    "cpu_usage": 0.9,
    "memory_usage": 42_000_000.0,
}

BASELINE = {
    "error_rate": 0.02,
    "request_rate": 0.7,
    "p95_latency": 0.1,
    "cpu_usage": 0.05,
    "memory_usage": 42_000_000.0,
}


def clear_rate_limiter():
    """Reset rate limiter state between tests."""
    _call_timestamps.clear()


# --- _fallback_explanation ---

class TestFallbackExplanation:
    def test_returns_string(self):
        result = _fallback_explanation(-0.7, METRICS, BASELINE)
        assert isinstance(result, str)

    def test_contains_score(self):
        result = _fallback_explanation(-0.7, METRICS, BASELINE)
        assert "-0.700" in result

    def test_identifies_worst_metric(self):
        result = _fallback_explanation(-0.7, METRICS, BASELINE)
        # error_rate deviation: (0.9-0.02)/0.02 = 44x
        # p95_latency deviation: (5.0-0.1)/0.1 = 49x — p95 wins
        assert "p95_latency" in result

    def test_handles_zero_baseline(self):
        baseline_zeros = {k: 0.0 for k in BASELINE}
        result = _fallback_explanation(-0.5, METRICS, baseline_zeros)
        assert isinstance(result, str)

    def test_handles_missing_metric_keys(self):
        result = _fallback_explanation(-0.5, {}, {})
        assert isinstance(result, str)


# --- _check_rate_limit ---

class TestCheckRateLimit:
    def setup_method(self):
        clear_rate_limiter()

    def test_allows_first_call(self):
        assert _check_rate_limit() is True

    def test_allows_up_to_max_calls(self):
        for _ in range(MAX_CLAUDE_CALLS_PER_HOUR):
            _check_rate_limit()
        # All should have been allowed — now we're at the limit
        assert len(_call_timestamps) == MAX_CLAUDE_CALLS_PER_HOUR

    def test_blocks_when_limit_reached(self):
        for _ in range(MAX_CLAUDE_CALLS_PER_HOUR):
            _check_rate_limit()
        assert _check_rate_limit() is False

    def test_evicts_old_timestamps(self):
        # Add old timestamp (2 hours ago)
        old_time = time.monotonic() - 7200
        _call_timestamps.append(old_time)
        # Should be evicted on next check
        result = _check_rate_limit()
        assert result is True
        assert old_time not in _call_timestamps


# --- explain_anomaly ---

class TestExplainAnomaly:
    def setup_method(self):
        clear_rate_limiter()

    def test_returns_fallback_when_no_api_key(self):
        with patch("src.explainer.ANTHROPIC_API_KEY", ""):
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_fallback_when_rate_limited(self):
        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"):
            # Exhaust rate limit
            for _ in range(MAX_CLAUDE_CALLS_PER_HOUR):
                _check_rate_limit()
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE)
        assert isinstance(result, str)

    def test_returns_claude_response_on_success(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="High error rate detected. Restart recommended.")]

        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"), \
             patch("src.explainer.client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE, incident_id="test123")

        assert result == "High error rate detected. Restart recommended."

    def test_returns_fallback_on_api_error(self):
        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"), \
             patch("src.explainer.client") as mock_client:
            mock_client.messages.create.side_effect = Exception("API error")
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE)

        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_fallback_on_empty_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="   ")]  # whitespace only

        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"), \
             patch("src.explainer.client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE)

        assert isinstance(result, str)
        assert len(result.strip()) > 0

    def test_returns_fallback_on_empty_content_list(self):
        mock_response = MagicMock()
        mock_response.content = []

        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"), \
             patch("src.explainer.client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = explain_anomaly(-0.7, -0.6, METRICS, BASELINE)

        assert isinstance(result, str)

    def test_incident_id_included_in_logs(self, caplog):
        import logging
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Some explanation")]

        with patch("src.explainer.ANTHROPIC_API_KEY", "test-key"), \
             patch("src.explainer.client") as mock_client, \
             caplog.at_level(logging.INFO, logger="src.explainer"):
            mock_client.messages.create.return_value = mock_response
            explain_anomaly(-0.7, -0.6, METRICS, BASELINE, incident_id="abc123")

        assert "abc123" in caplog.text

    def test_handles_missing_metric_keys_gracefully(self):
        with patch("src.explainer.ANTHROPIC_API_KEY", ""):
            result = explain_anomaly(-0.7, -0.6, {}, {})
        assert isinstance(result, str)
