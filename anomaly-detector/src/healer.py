import os
import logging
import threading
import time
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

HEALING_ENABLED = os.getenv("HEALING_ENABLED", "true").lower() == "true"
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "900"))  # 15 minutes
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "app")
TARGET_DEPLOYMENT = os.getenv("TARGET_DEPLOYMENT", "victim-service")

healing_duration = Histogram("anomaly_healing_duration_seconds", "Time taken to perform self-healing")
healing_counter = Counter("anomaly_healing_total", "Total self-healing actions", ["action", "status"])
healing_cooldown_gauge = Gauge("anomaly_healing_cooldown_active", "1 if healing cooldown is active")

# NOTE: In-process state only — resets on pod restart, not shared across replicas.
# Tech debt: persist to Deployment annotation or ConfigMap for multi-replica support.
_last_healing_time: float = 0.0
_healing_lock = threading.Lock()


def _is_cooldown_active() -> bool:
    """Check cooldown using monotonic clock. Thread-safe."""
    elapsed = time.monotonic() - _last_healing_time
    if elapsed < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - elapsed
        log.info("Healing cooldown active (%.0fs remaining)", remaining)
        healing_cooldown_gauge.set(1)
        return True
    healing_cooldown_gauge.set(0)
    return False


def rollout_restart(
    namespace: str = TARGET_NAMESPACE,
    deployment: str = TARGET_DEPLOYMENT,
    incident_id: str = "",
) -> bool:
    """Trigger rollout restart via Kubernetes Python SDK. Thread-safe."""
    global _last_healing_time

    if not HEALING_ENABLED:
        log.info("[%s] Healing disabled (HEALING_ENABLED=false) — skipping", incident_id)
        return False

    with _healing_lock:
        if _is_cooldown_active():
            return False

        # Import here to allow mocking in tests
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            from kubernetes.client.rest import ApiException
        except ImportError as e:
            log.error("[%s] kubernetes package not available: %s", incident_id, e)
            return False

        try:
            k8s_config.load_incluster_config()
        except Exception:
            try:
                k8s_config.load_kube_config()
            except Exception as e:
                log.error("[%s] Failed to load kubeconfig: %s", incident_id, e)
                return False

        try:
            apps_v1 = k8s_client.AppsV1Api()

            restarted_at = datetime.now(timezone.utc).isoformat()
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": restarted_at
                            }
                        }
                    }
                }
            }

            log.info(
                "[%s] Patching %s/%s with restartedAt=%s",
                incident_id, namespace, deployment, restarted_at
            )

            start = time.monotonic()
            apps_v1.patch_namespaced_deployment(
                name=deployment,
                namespace=namespace,
                body=patch,
            )
            duration = time.monotonic() - start

            healing_duration.observe(duration)
            healing_counter.labels(action="rollout_restart", status="success").inc()
            healing_cooldown_gauge.set(1)
            _last_healing_time = time.monotonic()

            log.info(
                "[%s] Rollout restart triggered for %s/%s (duration=%.2fs)",
                incident_id, namespace, deployment, duration
            )
            return True

        except ApiException as e:
            healing_counter.labels(action="rollout_restart", status="error").inc()
            log.error(
                "[%s] Kubernetes ApiException status=%s body=%s",
                incident_id, getattr(e, "status", None), getattr(e, "body", None)
            )
            return False

        except Exception as e:
            healing_counter.labels(action="rollout_restart", status="error").inc()
            log.error("[%s] Healing failed: %s", incident_id, e)
            return False
