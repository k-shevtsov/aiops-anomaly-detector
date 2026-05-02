"""
src/notifier.py  —  Telegram notifications with deduplication.

Updated to consume structured output from AgentResult:
  severity, root_cause, recommended_action, actions_taken.
All new fields are optional kwargs — backward compatible.
"""

import html
import logging
import os
import time
import threading
from collections import defaultdict

TELEGRAM_MAX_LENGTH = 4096  # Telegram API hard limit
import requests
from prometheus_client import Counter

log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEDUP_WINDOW     = int(os.getenv("DEDUP_WINDOW_SECONDS", "300"))  # 5 min

notify_total   = Counter("notifier_sends_total",  "Total Telegram sends attempted")
notify_errors  = Counter("notifier_errors_total",  "Total Telegram send errors")
notify_deduped = Counter("notifier_deduped_total", "Notifications skipped as duplicates")

_last_notification_time: dict[str, float] = defaultdict(float)
_dedup_lock = threading.Lock()

# Severity → emoji map
_SEVERITY_EMOJI = {
    "low":      "🟡",
    "medium":   "🟠",
    "high":     "🔴",
    "critical": "🚨",
    "unknown":  "⚪",
}


def _is_duplicate(incident_id: str) -> bool:
    with _dedup_lock:
        elapsed = time.monotonic() - _last_notification_time[incident_id]
        if elapsed < DEDUP_WINDOW:
            notify_deduped.inc()
            log.debug("Dedup: incident %s notified %.0fs ago", incident_id, elapsed)
            return True
        return False


def send_telegram(message: str, incident_id: str = "") -> bool:
    """Low-level Telegram send. Returns True on success."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[%s] Telegram credentials not set — skipping send", incident_id)
        return False

    # Truncate to Telegram API limit
    if len(message) > TELEGRAM_MAX_LENGTH:
        message = message[:TELEGRAM_MAX_LENGTH - 1] + "…"

    notify_total.inc()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        notify_errors.inc()
        log.error("[%s] Telegram send failed: %s", incident_id, exc)
        return False


def notify_anomaly(
    score:              float,
    threshold:          float,
    metrics:            dict[str, float],
    explanation:        str,
    healing_performed:  bool,
    incident_id:        str  = "",
    # ── New structured fields from AgentResult ────────────────────────────
    severity:           str        = "unknown",
    root_cause:         str        = "",
    recommended_action: str        = "",
    actions_taken:      list[str]  = None,
) -> None:
    """Send anomaly notification with deduplication and structured agent output."""
    if _is_duplicate(incident_id):
        return

    sev_emoji   = _SEVERITY_EMOJI.get(severity, "⚪")
    sev_label   = severity.upper() if severity != "unknown" else "UNKNOWN"

    healing_line = (
        "✅ Self-healing: rollout restart triggered"
        if healing_performed
        else "⏸ Self-healing: skipped (cooldown or disabled)"
    )

    # Escape any HTML-unsafe chars from LLM output
    safe_explanation = html.escape(explanation)
    safe_root_cause  = html.escape(root_cause)  if root_cause  else ""
    safe_rec_action  = html.escape(recommended_action) if recommended_action else ""

    # ── Build message sections ────────────────────────────────────────────
    lines = [
        f"{sev_emoji} <b>ANOMALY DETECTED — {sev_label}</b>",
        f"Incident: <code>{incident_id}</code>",
        "",
        f"Score: <b>{score:.3f}</b>  (threshold: {threshold:.3f})",
        "",
        "<b>Metrics snapshot:</b>",
        f"  Error rate:   {metrics.get('error_rate',   0.0):.3f} req/s",
        f"  Request rate: {metrics.get('request_rate', 0.0):.3f} req/s",
        f"  P95 latency:  {metrics.get('p95_latency',  0.0):.3f}s",
    ]

    if safe_root_cause:
        lines += ["", f"<b>Root cause:</b>", safe_root_cause]

    lines += ["", f"<b>Agent analysis:</b>", safe_explanation]

    if safe_rec_action:
        lines += ["", f"<b>Recommended action:</b>", safe_rec_action]

    if actions_taken:
        # Show up to 4 tool calls to keep message concise
        tool_lines = "\n".join(f"  • {a}" for a in actions_taken[:4])
        if len(actions_taken) > 4:
            tool_lines += f"\n  … and {len(actions_taken) - 4} more"
        lines += ["", f"<b>Agent tool calls ({len(actions_taken)}):</b>", tool_lines]

    lines += ["", healing_line]

    message = "\n".join(lines)

    ok = send_telegram(message, incident_id=incident_id)
    if ok:
        with _dedup_lock:
            _last_notification_time[incident_id] = time.monotonic()
    else:
        log.error("[%s] Failed to send anomaly notification to Telegram", incident_id)
