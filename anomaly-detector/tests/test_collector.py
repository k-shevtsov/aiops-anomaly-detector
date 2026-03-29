import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from src.collector import fetch_metric, collect_metrics, collect_features, QUERIES


# --- fetch_metric ---

class TestFetchMetric:
    def test_returns_float_on_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"value": [1234567890, "0.42"]}]}
        }
        with patch("src.collector.session.get", return_value=mock_resp):
            result = fetch_metric("some_query")
        assert result == pytest.approx(0.42)

    def test_returns_zero_on_empty_result(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"result": []}}
        with patch("src.collector.session.get", return_value=mock_resp):
            result = fetch_metric("some_query")
        assert result == 0.0

    def test_returns_zero_on_connection_error(self):
        with patch("src.collector.session.get", side_effect=ConnectionError("refused")):
            result = fetch_metric("some_query")
        assert result == 0.0

    def test_returns_zero_on_timeout(self):
        import requests
        with patch("src.collector.session.get", side_effect=requests.exceptions.Timeout):
            result = fetch_metric("some_query")
        assert result == 0.0

    def test_returns_zero_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("invalid json")
        with patch("src.collector.session.get", return_value=mock_resp):
            result = fetch_metric("some_query")
        assert result == 0.0

    def test_parses_large_float(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"value": [0, "42000000.0"]}]}
        }
        with patch("src.collector.session.get", return_value=mock_resp):
            result = fetch_metric("memory_query")
        assert result == pytest.approx(42_000_000.0)


# --- collect_metrics ---

class TestCollectMetrics:
    def _mock_fetch(self, values: dict):
        """Patch fetch_metric to return values by query keyword."""
        def side_effect(query):
            for key, val in values.items():
                if key in query:
                    return val
            return 0.0
        return side_effect

    def test_returns_dict_with_all_keys(self):
        with patch("src.collector.fetch_metric", return_value=0.1):
            result = collect_metrics()
        assert set(result.keys()) == {
            "error_rate", "request_rate", "p95_latency", "cpu_usage", "memory_usage"
        }

    def test_returns_float_values(self):
        with patch("src.collector.fetch_metric", return_value=1.23):
            result = collect_metrics()
        for v in result.values():
            assert isinstance(v, float)

    def test_collects_all_five_metrics(self):
        with patch("src.collector.fetch_metric", return_value=0.5) as mock_fetch:
            collect_metrics()
        assert mock_fetch.call_count == len(QUERIES)

    def test_handles_partial_failure(self):
        call_count = 0

        def flaky_fetch(query):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("flaky")
            return 0.1

        with patch("src.collector.fetch_metric", side_effect=flaky_fetch):
            # collect_metrics catches per-metric errors via fetch_metric
            result = collect_metrics()
        assert len(result) == 5


# --- collect_features ---

class TestCollectFeatures:
    def test_returns_ndarray(self):
        m = {"error_rate": 0.1, "request_rate": 1.0,
             "p95_latency": 0.2, "cpu_usage": 0.05, "memory_usage": 42_000_000.0}
        result = collect_features(m)
        assert isinstance(result, np.ndarray)

    def test_shape_is_1x5(self):
        m = {"error_rate": 0.1, "request_rate": 1.0,
             "p95_latency": 0.2, "cpu_usage": 0.05, "memory_usage": 42_000_000.0}
        result = collect_features(m)
        assert result.shape == (1, 5)

    def test_correct_feature_order(self):
        m = {"error_rate": 0.1, "request_rate": 2.0,
             "p95_latency": 0.3, "cpu_usage": 0.4, "memory_usage": 5.0}
        result = collect_features(m)
        assert result[0, 0] == pytest.approx(0.1)
        assert result[0, 1] == pytest.approx(2.0)
        assert result[0, 2] == pytest.approx(0.3)
        assert result[0, 3] == pytest.approx(0.4)
        assert result[0, 4] == pytest.approx(5.0)

    def test_zero_values_allowed(self):
        m = {"error_rate": 0.0, "request_rate": 0.0,
             "p95_latency": 0.0, "cpu_usage": 0.0, "memory_usage": 0.0}
        result = collect_features(m)
        assert result.shape == (1, 5)
        assert np.all(result == 0.0)
