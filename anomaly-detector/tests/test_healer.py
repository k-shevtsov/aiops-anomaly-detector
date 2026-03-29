import time
import pytest
from unittest.mock import patch, MagicMock, call

import src.healer as healer_module
from src.healer import rollout_restart, _is_cooldown_active, COOLDOWN_SECONDS


# --- Fixtures ---

def reset_healer():
    """Reset healer state between tests."""
    healer_module._last_healing_time = 0.0


# --- _is_cooldown_active ---

class TestIsCooldownActive:
    def setup_method(self):
        reset_healer()

    def test_inactive_at_start(self):
        assert _is_cooldown_active() is False

    def test_active_immediately_after_healing(self):
        healer_module._last_healing_time = time.monotonic()
        assert _is_cooldown_active() is True

    def test_inactive_after_cooldown_expired(self):
        healer_module._last_healing_time = time.monotonic() - COOLDOWN_SECONDS - 1
        assert _is_cooldown_active() is False

    def test_active_within_cooldown_window(self):
        healer_module._last_healing_time = time.monotonic() - (COOLDOWN_SECONDS // 2)
        assert _is_cooldown_active() is True

    def test_sets_gauge_1_when_active(self):
        healer_module._last_healing_time = time.monotonic()
        with patch.object(healer_module.healing_cooldown_gauge, "set") as mock_set:
            _is_cooldown_active()
        mock_set.assert_called_with(1)

    def test_sets_gauge_0_when_inactive(self):
        reset_healer()
        with patch.object(healer_module.healing_cooldown_gauge, "set") as mock_set:
            _is_cooldown_active()
        mock_set.assert_called_with(0)


# --- rollout_restart ---

class TestRolloutRestart:
    def setup_method(self):
        reset_healer()

    def _make_k8s_mocks(self):
        """Return mock k8s config and apps_v1 api."""
        mock_config = MagicMock()
        mock_config.ConfigException = Exception
        mock_apps_v1 = MagicMock()
        mock_k8s_client = MagicMock()
        mock_k8s_client.AppsV1Api.return_value = mock_apps_v1
        return mock_config, mock_k8s_client, mock_apps_v1

    def test_returns_false_when_healing_disabled(self):
        with patch("src.healer.HEALING_ENABLED", False):
            result = rollout_restart()
        assert result is False

    def test_returns_false_when_cooldown_active(self):
        healer_module._last_healing_time = time.monotonic()
        result = rollout_restart()
        assert result is False

    def test_returns_true_on_success(self):
        mock_config, mock_k8s_client, mock_apps_v1 = self._make_k8s_mocks()
        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=Exception),
            "kubernetes.config": mock_config,
        }):
            result = rollout_restart()
        assert result is True

    def test_updates_last_healing_time_on_success(self):
        mock_config, mock_k8s_client, mock_apps_v1 = self._make_k8s_mocks()
        before = time.monotonic()
        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=Exception),
            "kubernetes.config": mock_config,
        }):
            rollout_restart()
        assert healer_module._last_healing_time >= before

    def test_returns_false_on_api_exception(self):
        mock_config = MagicMock()
        mock_config.ConfigException = Exception

        class FakeApiException(Exception):
            def __init__(self):
                self.status = 403
                self.body = "Forbidden"

        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_deployment.side_effect = FakeApiException()
        mock_k8s_client = MagicMock()
        mock_k8s_client.AppsV1Api.return_value = mock_apps_v1

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=FakeApiException),
            "kubernetes.config": mock_config,
        }):
            result = rollout_restart()
        assert result is False

    def test_returns_false_on_generic_exception(self):
        mock_config = MagicMock()
        mock_config.ConfigException = Exception
        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_deployment.side_effect = RuntimeError("unexpected")
        mock_k8s_client = MagicMock()
        mock_k8s_client.AppsV1Api.return_value = mock_apps_v1

        # Use a distinct ApiException class so RuntimeError falls through to except Exception
        class FakeApiException(Exception):
            status = None
            body = None

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=FakeApiException),
            "kubernetes.config": mock_config,
        }):
            result = rollout_restart()
        assert result is False

    def test_does_not_update_healing_time_on_failure(self):
        mock_config = MagicMock()
        mock_config.ConfigException = Exception
        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_deployment.side_effect = RuntimeError("fail")
        mock_k8s_client = MagicMock()
        mock_k8s_client.AppsV1Api.return_value = mock_apps_v1

        # Use a distinct ApiException class so RuntimeError falls through to except Exception
        class FakeApiException(Exception):
            status = None
            body = None

        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=FakeApiException),
            "kubernetes.config": mock_config,
        }):
            rollout_restart()
        assert healer_module._last_healing_time == 0.0

    def test_patch_contains_restarted_at_annotation(self):
        mock_config, mock_k8s_client, mock_apps_v1 = self._make_k8s_mocks()
        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=Exception),
            "kubernetes.config": mock_config,
        }):
            rollout_restart(namespace="app", deployment="victim-service")

        patch_body = mock_apps_v1.patch_namespaced_deployment.call_args[1]["body"]
        annotations = patch_body["spec"]["template"]["metadata"]["annotations"]
        assert "kubectl.kubernetes.io/restartedAt" in annotations

    def test_cooldown_active_after_successful_restart(self):
        mock_config, mock_k8s_client, _ = self._make_k8s_mocks()
        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=Exception),
            "kubernetes.config": mock_config,
        }):
            rollout_restart()
        assert _is_cooldown_active() is True

    def test_incident_id_passed_through(self, caplog):
        import logging
        mock_config, mock_k8s_client, _ = self._make_k8s_mocks()
        with patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_k8s_client, config=mock_config),
            "kubernetes.client": mock_k8s_client,
            "kubernetes.client.rest": MagicMock(ApiException=Exception),
            "kubernetes.config": mock_config,
        }), caplog.at_level(logging.INFO, logger="src.healer"):
            rollout_restart(incident_id="xyz789")
        assert "xyz789" in caplog.text
