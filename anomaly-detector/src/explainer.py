import os
import html
import hashlib
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import anthropic
from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAX_CLAUDE_CALLS_PER_HOUR = int(os.getenv("MAX_CLAUDE_CALLS_PER_HOUR", "10"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
MAX_METRIC_VALUE_LENGTH = 20  # safety cap for injected values

# Prometheus metrics
llm_calls_total = Counter("explainer_llm_calls_total", "Total Claude API calls attempted")
llm_errors_total = Counter("explainer_llm_errors_total", "Total Claude API call errors")
llm_fallback_total = Counter("explainer_fallback_total", "Total times fallback explanation was used")
llm_rate_limit_blocked = Gauge("explainer_rate_limit_blocked", "1 if rate limit is currently blocking calls")

# Thread-safe rate limiter
_call_timestamps: deque = deque()
_rate_lock = threading.Lock()

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def _check_rate_limit() -> bool:
    """Return True if a Claude API call is allowed. Thread-safe."""
    now = time.monotonic()
    hour_ago = now - 3600
    with _rate_lock:
        while _call_timestamps and _call_timestamps[0] <= hour_ago:
            _call_timestamps.popleft()
        if len(_call_timestamps) >= MAX_CLAUDE_CALLS_PER_HOUR:
            log.warning("Claude API rate limit reached (%d calls/hour)", MAX_CLAUDE_CALLS_PER_HOUR)
            llm_rate_limit_blocked.set(1)
            return False
        _call_timestamps.append(now)
        llm_rate_limit_blocked.set(0)
        return True


def _safe_fmt(value: float, precision: int = 3) -> str:
    """Format float safely, capping string length."""
    return f"{value:.{precision}f}"[:MAX_METRIC_VALUE_LENGTH]


def explain_anomaly(
    score: float,
    threshold: float,
    metrics: dict[str, float],
    baseline: dict[str, float],
    incident_id: str = "",
) -> str:
    """Call Claude API to explain the detected anomaly. Falls back to rule-based on any error."""

    # 1. Check API key first — don't consume rate limit quota if key is missing
    if not ANTHROPIC_API_KEY or client is None:
        log.warning("[%s] ANTHROPIC_API_KEY not set — using fallback explanation", incident_id)
        llm_fallback_total.inc()
        return _fallback_explanation(score, metrics, baseline)

    # 2. Check rate limit
    if not _check_rate_limit():
        llm_fallback_total.inc()
        return _fallback_explanation(score, metrics, baseline)

    llm_calls_total.inc()

    prompt = (
        f"You are an expert SRE analyzing a Kubernetes anomaly detected by an ML model.\n\n"
        f"Anomaly score: {_safe_fmt(score)} (threshold: {_safe_fmt(threshold)}, lower = more anomalous)\n"
        f"Detection time: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Current metrics vs baseline:\n"
        f"- Error rate: {_safe_fmt(metrics.get('error_rate', 0.0))} req/s "
        f"(baseline: {_safe_fmt(baseline.get('error_rate', 0.0))})\n"
        f"- Request rate: {_safe_fmt(metrics.get('request_rate', 0.0))} req/s "
        f"(baseline: {_safe_fmt(baseline.get('request_rate', 0.0))})\n"
        f"- P95 latency: {_safe_fmt(metrics.get('p95_latency', 0.0))}s "
        f"(baseline: {_safe_fmt(baseline.get('p95_latency', 0.0))}s)\n"
        f"- CPU usage: {_safe_fmt(metrics.get('cpu_usage', 0.0), 4)} "
        f"(baseline: {_safe_fmt(baseline.get('cpu_usage', 0.0), 4)})\n"
        f"- Memory: {metrics.get('memory_usage', 0.0) / 1024 / 1024:.1f}MB "
        f"(baseline: {baseline.get('memory_usage', 0.0) / 1024 / 1024:.1f}MB)\n\n"
        f"The ML model flagged this BEFORE any alert fired.\n\n"
        f"Provide:\n"
        f"1. Root cause hypothesis (2-3 sentences)\n"
        f"2. Which metric deviated most and why it matters\n"
        f"3. Recommended action (1 sentence)\n\n"
        f"Plain text only, no markdown, no bullet points."
    )

    start = time.monotonic()
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start

        # Guard against unexpected response structure
        if not response.content or not response.content[0].text.strip():
            log.error("[%s] Claude returned empty response (latency=%.2fs)", incident_id, latency)
            llm_errors_total.inc()
            llm_fallback_total.inc()
            return _fallback_explanation(score, metrics, baseline)

        explanation = response.content[0].text.strip()
        log.info(
            "[%s] Claude explanation received (%d chars, latency=%.2fs)",
            incident_id, len(explanation), latency
        )
        return explanation

    except Exception as e:
        latency = time.monotonic() - start
        llm_errors_total.inc()
        llm_fallback_total.inc()
        log.error("[%s] Claude API error (latency=%.2fs): %s — using fallback", incident_id, latency, e)
        return _fallback_explanation(score, metrics, baseline)


def _fallback_explanation(
    score: float,
    metrics: dict[str, float],
    baseline: dict[str, float],
) -> str:
    """Rule-based fallback when Claude API is unavailable."""
    worst_metric = max(
        ["error_rate", "p95_latency"],
        key=lambda m: abs(metrics.get(m, 0) - baseline.get(m, 0)) / max(baseline.get(m, 0.001), 0.001)
    )
    return (
        f"Anomaly detected (score={score:.3f}). "
        f"Most deviated metric: {worst_metric} "
        f"(current={metrics.get(worst_metric, 0):.3f}, "
        f"baseline={baseline.get(worst_metric, 0):.3f}). "
        f"Recommended action: investigate victim-service logs and consider rollout restart."
    )
