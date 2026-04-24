import asyncio
import hashlib
import os
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from src.collector import collect_metrics, collect_features
from src.model import AnomalyDetector
from src.agent import run_agent, AgentResult        # ← replaces explain_anomaly + rollout_restart
from src.notifier import notify_anomaly

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

MIN_TRAINING_SAMPLES    = int(os.getenv("MIN_TRAINING_SAMPLES",    "20"))
MIN_TRAINING_SECONDS    = int(os.getenv("MIN_TRAINING_SECONDS",    "600"))
MAX_TRAINING_SECONDS    = int(os.getenv("MAX_TRAINING_SECONDS",    "1800"))
SCRAPE_INTERVAL_SECONDS = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "30"))

detector  = AnomalyDetector(min_training_samples=MIN_TRAINING_SAMPLES)
app_state = {
    "phase":              "training",
    "training_start":     None,
    "anomalies_detected": 0,
    "active_tasks":       0,
}


def _make_incident_id(score: float, timestamp: datetime) -> str:
    """Generate stable incident ID from score + minute-level timestamp."""
    raw = f"{timestamp.strftime('%Y%m%d%H%M')}:{score:.3f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


async def handle_anomaly(
    score:       float,
    metrics:     dict[str, float],
    incident_id: str,
) -> None:
    """
    Run agentic analysis → notify.

    Claude now decides autonomously:
      - what Prometheus queries to run
      - whether to inspect pod logs
      - whether to trigger a rollout restart

    The AgentResult carries all structured output (severity, root_cause, etc.)
    for the notifier to use.
    """
    app_state["active_tasks"] += 1
    try:
        # Agentic loop — Claude reasons over live data with tool access
        result: AgentResult = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: run_agent(
                score       = score,
                threshold   = detector.threshold,
                metrics     = metrics,
                baseline    = detector.baseline,
                incident_id = incident_id,
            )
        )

        log.info(
            "[%s] Agent result: severity=%s healing=%s tool_calls=%d fallback=%s",
            incident_id,
            result.severity,
            result.healing_performed,
            result.tool_calls_made,
            result.fallback,
        )
        log.info("[%s] Root cause:  %s", incident_id, result.root_cause or "(unknown)")
        log.info("[%s] Explanation: %s", incident_id, result.explanation)

        # Notify — pass structured fields so the notifier can format a richer message
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: notify_anomaly(
                score              = score,
                threshold          = detector.threshold,
                metrics            = metrics,
                explanation        = result.explanation,
                healing_performed  = result.healing_performed,
                incident_id        = incident_id,
                # Extended fields available for notifier to use if desired:
                severity           = result.severity,
                root_cause         = result.root_cause,
                recommended_action = result.recommended_action,
                actions_taken      = result.actions_taken,
            )
        )
    except Exception as exc:
        log.error("[%s] handle_anomaly failed: %s", incident_id, exc)
    finally:
        app_state["active_tasks"] -= 1


async def training_phase():
    """Collect baseline until both MIN_TRAINING_SAMPLES and MIN_TRAINING_SECONDS are met."""
    app_state["phase"]          = "training"
    app_state["training_start"] = datetime.now(timezone.utc)
    log.info("=== TRAINING PHASE STARTED ===")
    log.info(
        "Target: >= %d samples AND >= %ds (fail-safe: %ds)",
        MIN_TRAINING_SAMPLES, MIN_TRAINING_SECONDS, MAX_TRAINING_SECONDS,
    )

    elapsed = 0
    while elapsed < MAX_TRAINING_SECONDS:
        metrics  = collect_metrics()
        features = collect_features(metrics)
        detector.add_training_sample(features)

        log.info(
            "Training sample #%d collected (elapsed=%ds, target=%ds)",
            len(detector.training_data), elapsed, MIN_TRAINING_SECONDS,
        )

        elapsed += SCRAPE_INTERVAL_SECONDS
        if len(detector.training_data) >= MIN_TRAINING_SAMPLES and elapsed >= MIN_TRAINING_SECONDS:
            log.info(
                "Conditions met: %d samples, %ds elapsed — training model…",
                len(detector.training_data), elapsed,
            )
            if detector.train():
                log.info("Training successful.")
                log.info("Baseline: %s", detector.baseline)
                log.info("Dynamic threshold: %.3f", detector.threshold)
                return
            else:
                log.error("Training failed despite sufficient samples — will retry next cycle")

        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)

    raise RuntimeError(
        f"Training failed: could not collect enough valid samples in {MAX_TRAINING_SECONDS}s"
    )


async def inference_phase():
    """Detect anomalies every SCRAPE_INTERVAL_SECONDS. Offload handling to background tasks."""
    app_state["phase"] = "inference"
    log.info("=== INFERENCE PHASE STARTED ===")

    while True:
        metrics  = collect_metrics()
        features = collect_features(metrics)
        score, is_anomaly = detector.predict(features)

        log.info("score=%.3f threshold=%.3f anomaly=%s", score, detector.threshold, is_anomaly)

        if is_anomaly:
            app_state["anomalies_detected"] += 1
            now         = datetime.now(timezone.utc)
            incident_id = _make_incident_id(score, now)

            log.warning(
                "ANOMALY DETECTED! score=%.3f threshold=%.3f incident_id=%s metrics=%s",
                score, detector.threshold, incident_id, metrics,
            )

            # Fire and forget — does not block detection loop
            asyncio.create_task(handle_anomaly(score, metrics, incident_id))

        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


async def main_loop():
    await training_phase()
    await inference_phase()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(main_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="AIOps Anomaly Detector", lifespan=lifespan)
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/health/live")
async def liveness():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    return {
        "status":             "ok",
        "phase":              app_state["phase"],
        "is_trained":         detector.is_trained,
        "anomalies_detected": app_state["anomalies_detected"],
    }


@app.get("/status")
async def status():
    return {
        "phase":              app_state["phase"],
        "training_samples":   len(detector.training_data),
        "is_trained":         detector.is_trained,
        "baseline":           detector.baseline,
        "anomalies_detected": app_state["anomalies_detected"],
        "active_tasks":       app_state["active_tasks"],
        "threshold":          detector.threshold,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
