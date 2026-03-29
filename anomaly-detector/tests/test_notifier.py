import time
import pytest
from unittest.mock import patch, MagicMock

import src.notifier as notifier_module
from src.notifier import send_telegram, notify_anomaly, TELEGRAM_MAX_LENGTH


# --- Fixtures ---

METRICS = {
    "error_rate": 0.9,
    "request_rate": 0.5,
    "p95_latency": 3.5,
    "cpu_usage": 0.0,
    "memory_usage": 42_000_000.0,
}

EXPLANATION = "High error rate detected. Service is degraded."


def reset_notifier():
    """Reset deduplication state between tests."""
    notifier_module._last_notification_time.clear()


# --- send_telegram ---

class TestSendTelegram:
    def test_returns_false_when_no_token(self):
        with patch("src.notifier.TELEGRAM_TOKEN", ""), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"):
            result = send_telegram("test message")
        assert result is False

    def test_returns_false_when_no_chat_id(self):
        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", ""):
            result = send_telegram("test message")
        assert result is False

    def test_returns_true_on_success(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", return_value=mock_resp):
            result = send_telegram("test message")
        assert result is True

    def test_returns_false_on_http_error(self):
        import requests
        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=requests.exceptions.HTTPError):
            result = send_telegram("test message")
        assert result is False

    def test_returns_false_on_connection_error(self):
        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=ConnectionError):
            result = send_telegram("test message")
        assert result is False

    def test_truncates_long_message(self):
        long_message = "x" * 5000
        captured = {}

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            send_telegram(long_message)

        assert len(captured["text"]) <= TELEGRAM_MAX_LENGTH + 1  # +1 for ellipsis
        assert captured["text"].endswith("…")

    def test_short_message_not_truncated(self):
        short_message = "short"
        captured = {}

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            send_telegram(short_message)

        assert captured["text"] == short_message

    def test_sends_with_html_parse_mode(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["json"] = json
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            send_telegram("test")

        assert captured["json"]["parse_mode"] == "HTML"


# --- notify_anomaly ---

class TestNotifyAnomaly:
    def setup_method(self):
        reset_notifier()

    def _call_notify(self, incident_id="inc001", healing=False):
        notify_anomaly(
            score=-0.7,
            threshold=-0.6,
            metrics=METRICS,
            explanation=EXPLANATION,
            healing_performed=healing,
            incident_id=incident_id,
        )

    def test_sends_notification_on_first_call(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", return_value=mock_resp) as mock_post:
            self._call_notify()

        mock_post.assert_called_once()

    def test_deduplicates_second_call_same_incident(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", return_value=mock_resp) as mock_post:
            self._call_notify(incident_id="inc001")
            self._call_notify(incident_id="inc001")

        assert mock_post.call_count == 1

    def test_sends_for_different_incidents(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", return_value=mock_resp) as mock_post:
            self._call_notify(incident_id="inc001")
            self._call_notify(incident_id="inc002")

        assert mock_post.call_count == 2

    def test_escapes_html_in_explanation(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            notify_anomaly(
                score=-0.7,
                threshold=-0.6,
                metrics=METRICS,
                explanation="Error: <script>alert('xss')</script> & more",
                healing_performed=False,
                incident_id="inc_html",
            )

        assert "<script>" not in captured["text"]
        assert "&lt;script&gt;" in captured["text"]
        assert "&amp;" in captured["text"]

    def test_message_contains_incident_id(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            self._call_notify(incident_id="abc123")

        assert "abc123" in captured["text"]

    def test_message_contains_healing_status_true(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            self._call_notify(healing=True)

        assert "rollout restart triggered" in captured["text"]

    def test_message_contains_healing_status_false(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        def capture_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return mock_resp

        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=capture_post):
            self._call_notify(healing=False)

        assert "skipped" in captured["text"]

    def test_logs_error_on_failed_send(self, caplog):
        import logging
        with patch("src.notifier.TELEGRAM_TOKEN", "token"), \
             patch("src.notifier.TELEGRAM_CHAT_ID", "123"), \
             patch("src.notifier.requests.post", side_effect=Exception("fail")), \
             caplog.at_level(logging.ERROR, logger="src.notifier"):
            self._call_notify()

        assert "Failed" in caplog.text or "failed" in caplog.text.lower()
