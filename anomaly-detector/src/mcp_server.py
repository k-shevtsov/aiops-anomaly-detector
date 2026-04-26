"""
src/mcp_server.py  —  MCP Server for AIOps Anomaly Detector (Tier-3 Step-2).

Exposes the running anomaly detector to any MCP client:
  - Claude Desktop (local STDIO transport)
  - Claude.ai with MCP connectors (HTTP transport)
  - Any MCP-compatible LLM client

Tools exposed:
  get_anomaly_status()          → current score, phase, baseline
  get_recent_incidents(n)       → last N incidents from RAG store
  get_prometheus_metric(query)  → run arbitrary PromQL against cluster
  trigger_manual_analysis()     → force an agent analysis run right now
  get_pod_logs(namespace, app)  → fetch live pod logs

Resources exposed:
  detector://status             → live detector status (JSON)
  detector://incidents          → recent incidents from RAG store

Transports:
  STDIO  — for Claude Desktop local integration
  HTTP   — for remote / claude.ai integration (run with --http flag)

Usage:
  # STDIO (Claude Desktop):
  python src/mcp_server.py

  # HTTP (remote clients):
  python src/mcp_server.py --http --port 8002

Claude Desktop config (~/.config/claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "aiops-anomaly-detector": {
        "command": "python",
        "args": ["/path/to/anomaly-detector/src/mcp_server.py"],
        "env": {
          "PROMETHEUS_URL": "http://localhost:9090",
          "SQLITE_DB_PATH": "/path/to/incidents.db"
        }
      }
    }
  }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# ── Path setup — works both locally and in container ─────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
PROMETHEUS_URL   = os.getenv("PROMETHEUS_URL",  "http://localhost:9090")
DETECTOR_URL     = os.getenv("DETECTOR_URL",    "http://localhost:8001")
SQLITE_DB_PATH   = os.getenv("SQLITE_DB_PATH",  "/data/incidents.db")
MCP_SERVER_NAME  = "aiops-anomaly-detector"
MCP_SERVER_DESC  = (
    "AIOps Anomaly Detector — query live Kubernetes cluster health, "
    "review past incidents, run Prometheus queries, and trigger analysis."
)

# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP(MCP_SERVER_NAME, instructions=MCP_SERVER_DESC)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _http_get(url: str, params: dict | None = None, timeout: int = 5) -> dict:
    """Simple HTTP GET — avoids adding httpx dependency."""
    import urllib.request, urllib.parse
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _promql(query: str) -> list[dict]:
    """Run an instant PromQL query, return result list."""
    data = _http_get(f"{PROMETHEUS_URL}/api/v1/query", {"query": query})
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {data.get('error', 'unknown')}")
    return data["data"]["result"]


def _detector_status() -> dict:
    """Fetch /status from the running anomaly detector."""
    try:
        return _http_get(f"{DETECTOR_URL}/status")
    except Exception as exc:
        return {"error": str(exc), "note": "detector may not be running locally"}


def _get_rag_incidents(n: int = 5) -> list[dict]:
    """Pull recent incidents from the RAG SQLite store."""
    try:
        import sqlite3
        if not os.path.exists(SQLITE_DB_PATH):
            return []
        conn = sqlite3.connect(SQLITE_DB_PATH)
        rows = conn.execute(
            "SELECT payload FROM incidents ORDER BY created_at DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_anomaly_status() -> dict:
    """
    Get the current state of the anomaly detector.

    Returns the detection phase (training/inference), current anomaly score,
    dynamic threshold, baseline metrics, and number of anomalies detected.
    Use this to understand if the system is healthy and what the current
    risk level is.
    """
    status = _detector_status()

    # Enrich with live Prometheus score if detector is in inference phase
    if status.get("phase") == "inference" and status.get("is_trained"):
        try:
            results = _promql("anomaly_detector_score")
            if results:
                status["live_score"] = float(results[0]["value"][1])
        except Exception:
            pass

    return status


@mcp.tool()
def get_recent_incidents(n: int = 5) -> list[dict]:
    """
    Retrieve the N most recent anomaly incidents from the incident store.

    Each incident contains: incident_id, timestamp, severity, root_cause,
    metrics_snapshot, explanation, recommended_action, healing_performed.

    Use this to understand recent incident history and patterns.

    Args:
        n: Number of incidents to return (default 5, max 20)
    """
    n = min(max(1, n), 20)
    incidents = _get_rag_incidents(n)
    if not incidents:
        return [{"message": "No incidents stored yet. Incidents are saved after each detected anomaly."}]
    return incidents


@mcp.tool()
def get_prometheus_metric(promql: str, lookback_minutes: int = 0) -> dict:
    """
    Run a PromQL query against the cluster Prometheus instance.

    Use for real-time metric investigation. Examples:
      - error rate:  rate(http_requests_total{job="victim-service",status="500"}[1m])
      - p95 latency: histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="victim-service"}[1m])) by (le))
      - cpu usage:   rate(container_cpu_usage_seconds_total{pod=~"victim-service.*"}[1m])

    Args:
        promql: PromQL expression to evaluate
        lookback_minutes: If >0, returns a range query over the last N minutes
    """
    try:
        if lookback_minutes > 0:
            end   = time.time()
            start = end - lookback_minutes * 60
            data  = _http_get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                {"query": promql, "start": start, "end": end, "step": "30s"},
            )
        else:
            data = _http_get(
                f"{PROMETHEUS_URL}/api/v1/query",
                {"query": promql},
            )

        if data.get("status") != "success":
            return {"error": data.get("error", "prometheus query failed"), "promql": promql}

        results = data["data"]["result"]
        # Cap results to keep response manageable
        return {
            "promql":       promql,
            "result_count": len(results),
            "results":      results[:10],
            "truncated":    len(results) > 10,
        }
    except Exception as exc:
        return {"error": str(exc), "promql": promql}


@mcp.tool()
def get_pod_logs(namespace: str = "app", label_selector: str = "app=victim-service",
                 tail_lines: int = 50) -> dict:
    """
    Fetch recent log lines from Kubernetes pods.

    Use to investigate errors, crashes, OOM kills, or unexpected behaviour.
    Requires kubectl access (works when running in-cluster or with kubeconfig).

    Args:
        namespace:      Kubernetes namespace (default: "app")
        label_selector: Pod label selector (default: "app=victim-service")
        tail_lines:     Number of recent log lines to return (max 200)
    """
    tail_lines = min(tail_lines, 200)
    try:
        from kubernetes import client as k8s, config as k8s_cfg
        try:
            k8s_cfg.load_incluster_config()
        except Exception:
            k8s_cfg.load_kube_config()

        v1   = k8s.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

        if not pods.items:
            return {"error": f"No pods found for selector '{label_selector}' in {namespace}"}

        results = {}
        for pod in pods.items[:3]:   # max 3 pods
            pod_name = pod.metadata.name
            try:
                logs = v1.read_namespaced_pod_log(
                    name=pod_name, namespace=namespace, tail_lines=tail_lines
                )
                results[pod_name] = logs[-3000:]   # cap at 3KB
            except Exception as exc:
                results[pod_name] = f"Error: {exc}"

        return {"namespace": namespace, "selector": label_selector, "pods": results}

    except ImportError:
        return {"error": "kubernetes package not available in this environment"}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def trigger_manual_analysis(reason: str = "manual trigger from Claude Desktop") -> dict:
    """
    Force an immediate anomaly analysis run using the current live metrics.

    This calls the agent directly (outside the normal detection loop) and
    returns the full AgentResult including severity, root cause, and whether
    healing was performed.

    Use when you want to ask Claude to investigate the cluster right now,
    without waiting for the next detection cycle.

    Args:
        reason: Why you're triggering this analysis (logged for audit trail)
    """
    try:
        from collector import collect_metrics, collect_features
        from model import AnomalyDetector
        from agent import run_agent

        log.info("Manual analysis triggered: %s", reason)

        # Collect live metrics
        metrics = collect_metrics()

        # Use a minimal detector just for scoring — we won't train, just use
        # the live metrics as-is and run the agent directly
        incident_id = f"manual-{datetime.now(timezone.utc).strftime('%H%M%S')}"

        # Run the agent with a neutral score/threshold (not from trained model)
        # The agent will use its tools to assess severity independently
        result = run_agent(
            score       = -0.5,     # neutral — agent decides severity via tools
            threshold   = -0.5,
            metrics     = metrics,
            baseline    = {k: 0.0 for k in metrics},
            incident_id = incident_id,
        )

        return {
            "incident_id":        incident_id,
            "triggered_by":       reason,
            "timestamp":          datetime.now(timezone.utc).isoformat(),
            "metrics_snapshot":   metrics,
            "severity":           result.severity,
            "root_cause":         result.root_cause,
            "explanation":        result.explanation,
            "recommended_action": result.recommended_action,
            "healing_performed":  result.healing_performed,
            "tool_calls_made":    result.tool_calls_made,
            "fallback":           result.fallback,
        }

    except Exception as exc:
        return {
            "error":   str(exc),
            "message": "Manual analysis failed — is the detector running?",
        }


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("detector://status")
def resource_detector_status() -> str:
    """
    Live anomaly detector status as JSON.
    Includes phase, score, threshold, baseline, and anomaly count.
    """
    return json.dumps(_detector_status(), indent=2, default=str)


@mcp.resource("detector://incidents")
def resource_recent_incidents() -> str:
    """
    Recent anomaly incidents from the RAG store (last 10).
    Each entry includes severity, root cause, metrics, and resolution.
    """
    incidents = _get_rag_incidents(10)
    return json.dumps(incidents, indent=2, default=str)


@mcp.resource("detector://prometheus-targets")
def resource_prometheus_targets() -> str:
    """
    List of active Prometheus scrape targets and their health status.
    """
    try:
        data = _http_get(f"{PROMETHEUS_URL}/api/v1/targets")
        active = data.get("data", {}).get("activeTargets", [])
        summary = [
            {
                "job":       t.get("labels", {}).get("job"),
                "instance":  t.get("labels", {}).get("instance"),
                "health":    t.get("health"),
                "lastError": t.get("lastError"),
                "scrapeUrl": t.get("scrapeUrl"),
            }
            for t in active
        ]
        return json.dumps(summary, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def investigate_anomaly(incident_id: str = "") -> str:
    """
    Generate a prompt for investigating a specific incident or the current state.

    Args:
        incident_id: Optional incident ID to investigate. Leave empty for current state.
    """
    if incident_id:
        return (
            f"Please investigate incident {incident_id} using the available tools.\n\n"
            f"1. Use get_recent_incidents() to find the incident details.\n"
            f"2. Use get_prometheus_metric() to check current metric state.\n"
            f"3. Use get_pod_logs() to check for related errors.\n"
            f"4. Provide a root cause analysis and recommended next steps."
        )
    return (
        "Please assess the current health of the Kubernetes cluster.\n\n"
        "1. Use get_anomaly_status() to check the detector state.\n"
        "2. Use get_prometheus_metric() with relevant PromQL to check key metrics.\n"
        "3. Use get_recent_incidents() to understand recent history.\n"
        "4. Summarise the cluster health and any recommended actions."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIOps MCP Server")
    parser.add_argument("--http", action="store_true",
                        help="Run with HTTP transport (default: STDIO for Claude Desktop)")
    parser.add_argument("--port", type=int, default=8002,
                        help="HTTP port (default: 8002, only used with --http)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="HTTP host (default: 0.0.0.0, only used with --http)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        stream=sys.stderr,
    )

    if args.http:
        log.info("Starting MCP server (HTTP) on %s:%d", args.host, args.port)
        log.info("Connect Claude Desktop to: http://%s:%d/mcp", args.host, args.port)
        # host/port must be passed to FastMCP constructor, not run()
        _http_mcp = FastMCP(
            MCP_SERVER_NAME,
            instructions = MCP_SERVER_DESC,
            host         = args.host,
            port         = args.port,
        )
        # Copy registered handlers from the module-level mcp instance
        _http_mcp._tool_manager     = mcp._tool_manager
        _http_mcp._resource_manager = mcp._resource_manager
        _http_mcp._prompt_manager   = mcp._prompt_manager
        _http_mcp.run(transport="streamable-http")
    else:
        log.info("Starting MCP server (STDIO) — waiting for client connection")
        mcp.run(transport="stdio")
