import os
import html
import logging
import time
import requests
from prometheus_client import Counter

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_MAX_LENGTH = 4000  # Telegram hard limit is 4096
NOTIFICATION_COOLDOWN_SECONDS = int(os.getenv("NOTIFICATION_COOLDOWN_SECONDS", "300"))  # 5 min

# Prometheus metrics
notifier_sent_total = Counter("notifier_sent_total", "Total Telegram notifications sent")
notifier_failed_total = Counter("notifier_failed_total", "Total Telegram notification failures")

# Simple deduplication — last notification time per incident fingerprint
_last_notification_time: dict[str, float] = {}


def _is_duplicate(incident_id: str) -> bool:
    """Return True if we notified about this incident recently."""
    last = _last_notification_time.get(incident_id, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < NOTIFICATION_COOLDOWN_SECONDS:
        log.info(
            "[%s] Notification deduplicated (%.0fs since last)",
            incident_id, elapsed
        )
        return True
    return False


def send_telegram(message: str, incident_id: str = "") -> bool:
    """Send HTML-formatted message to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[%s] Telegram not configured — skipping notification", incident_id)
        return False

    if len(message) > TELEGRAM_MAX_LENGTH:
        message = message[:TELEGRAM_MAX_LENGTH] + "…"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        notifier_sent_total.inc()
        log.info("[%s] Telegram notification sent", incident_id)
        return True
    except Exception as e:
        notifier_failed_total.inc()
        log.error("[%s] Telegram notification failed: %s", incident_id, e)
        return False


def notify_anomaly(
    score: float,
    threshold: float,
    metrics: dict[str, float],
    explanation: str,
    healing_performed: bool,
    incident_id: str = "",
) -> None:
    """Send anomaly notification with deduplication."""
    if _is_duplicate(incident_id):
        return

    healing_status = (
        "Self-healing: rollout restart triggered"
        if healing_performed
        else "Self-healing: skipped (cooldown or disabled)"
    )

    # Escape LLM output — may contain <, >, & which break HTML parse_mode
    safe_explanation = html.escape(explanation)

    message = (
        f"<b>ANOMALY DETECTED</b>\n"
        f"Incident: <code>{incident_id}</code>\n\n"
        f"Score: {score:.3f} (threshold: {threshold:.3f})\n\n"
        f"<b>Metrics:</b>\n"
        f"Error rate: {metrics.get('error_rate', 0.0):.3f} req/s\n"
        f"Request rate: {metrics.get('request_rate', 0.0):.3f} req/s\n"
        f"P95 latency: {metrics.get('p95_latency', 0.0):.3f}s\n\n"
        f"<b>Explanation:</b>\n{safe_explanation}\n\n"
        f"{healing_status}"
    )

    ok = send_telegram(message, incident_id=incident_id)
    if ok:
        _last_notification_time[incident_id] = time.monotonic()
    else:
        log.error("[%s] Failed to send anomaly notification to Telegram", incident_id)
