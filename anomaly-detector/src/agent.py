"""
src/agent.py  —  Agentic anomaly analyser with Claude Tool Use.

Replaces the fire-and-forget explain_anomaly() + rollout_restart() duo.
Claude now DECIDES what data to gather and whether to heal.

Agentic loop:
  1. Initial prompt with metric snapshot
  2. Claude calls tools (query_prometheus, get_pod_logs, …, restart_deployment)
  3. Loop until stop_reason == "end_turn" OR max iterations reached
  4. Final message parsed into AgentResult (structured output, Tier-1 Step-2)
  5. Every run + iteration traced in Langfuse (LLM observability, Tier-1 Step-3)
"""

import json
import logging
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field

import anthropic
import requests
from prometheus_client import Counter, Histogram, Counter as _C

# ── Langfuse (optional — graceful no-op when not configured) ────────────────
# Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY + LANGFUSE_HOST in env.
# If keys are absent the _Tracer below is a no-op context manager — zero
# overhead, no import errors, existing tests stay green.
try:
    from langfuse import Langfuse as _LF
    _langfuse_client = _LF() if os.getenv("LANGFUSE_PUBLIC_KEY") else None
except ImportError:
    _langfuse_client = None

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
CLAUDE_MODEL          = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
PROMETHEUS_URL        = os.getenv("PROMETHEUS_URL", "http://prometheus-server:9090")
TARGET_NAMESPACE      = os.getenv("TARGET_NAMESPACE", "app")
TARGET_DEPLOYMENT     = os.getenv("TARGET_DEPLOYMENT", "victim-service")
MAX_AGENT_ITERATIONS  = int(os.getenv("MAX_AGENT_ITERATIONS", "5"))
MAX_CLAUDE_CALLS_PER_HOUR = int(os.getenv("MAX_CLAUDE_CALLS_PER_HOUR", "10"))

# ── Prometheus instrumentation ───────────────────────────────────────────────
agent_runs_total      = Counter("agent_runs_total", "Total agentic analysis runs")
agent_tool_calls      = Counter("agent_tool_calls_total", "Tool calls by the agent", ["tool_name"])
agent_errors_total    = Counter("agent_errors_total", "Agent errors", ["reason"])
agent_latency         = Histogram("agent_latency_seconds", "End-to-end agent latency")
agent_input_tokens    = Counter("agent_input_tokens_total",  "Claude input  tokens consumed")
agent_output_tokens   = Counter("agent_output_tokens_total", "Claude output tokens consumed")


# ── Langfuse tracer context-manager (v4 API) ────────────────────────────────
class _Tracer:
    """
    Thin wrapper around Langfuse v4 API.
    Creates one agent-span per incident; each LLM call is a nested generation.
    Degrades silently to a no-op when Langfuse is not configured.

    v4 key changes vs v2:
      - No .trace() / .span() / .generation() on the client directly.
      - Use client.start_observation(as_type='agent') for the root span,
        then span.start_observation(as_type='span'/'generation') for children.
      - Token counts go into usage_details={"input": N, "output": M}.
      - Trace-level I/O set via span.set_trace_io(input=..., output=...).
      - client.flush() → client.shutdown() in v4 (flush still works as alias).
    """

    def __init__(self, incident_id: str, score: float, metrics: dict):
        self._incident_id = incident_id
        self._score       = score
        self._metrics     = metrics
        self._root_span   = None   # LangfuseAgent span — lives for the whole run

    def __enter__(self):
        lf = _langfuse_client   # read module-level global at call time, not import time
        if lf:
            try:
                trace_id = lf.create_trace_id(seed=self._incident_id)
                self._root_span = lf.start_observation(
                    trace_context = {"trace_id": trace_id},   # dict accepted by v4
                    name          = "anomaly-agent",
                    as_type       = "agent",
                    input         = {"score": self._score, "metrics": self._metrics},
                    metadata      = {
                        "model":      CLAUDE_MODEL,
                        "deployment": TARGET_DEPLOYMENT,
                        "namespace":  TARGET_NAMESPACE,
                    },
                )
            except Exception as exc:
                log.debug("Langfuse root span creation failed (non-fatal): %s", exc)
                self._root_span = None
        return self

    def span(self, name: str, input_data: dict | None = None):
        """Open a child span on the root agent span. Returns handle or None."""
        if self._root_span:
            try:
                return self._root_span.start_observation(
                    name    = name,
                    as_type = "span",
                    input   = input_data or {},
                )
            except Exception as exc:
                log.debug("Langfuse span creation failed (non-fatal): %s", exc)
        return None

    def generation(
        self,
        name:        str,
        model:       str,
        input_msgs:  list,
        output_text: str,
        usage:       dict | None = None,
        metadata:    dict | None = None,
    ):
        """
        Record a model generation as a child of the root agent span.
        usage = {"input": N, "output": M} → maps to Langfuse usage_details.
        """
        if self._root_span:
            try:
                gen = self._root_span.start_observation(
                    name          = name,
                    as_type       = "generation",
                    model         = model,
                    input         = input_msgs,
                    output        = output_text,
                    usage_details = usage,      # v4 key (not `usage`)
                    metadata      = metadata or {},
                )
                gen.end()
            except Exception as exc:
                log.debug("Langfuse generation recording failed (non-fatal): %s", exc)

    def end_span(self, span, output: dict | None = None):
        """Close a span with optional output data."""
        if span:
            try:
                if output:
                    span.update(output=output)
                span.end()
            except Exception:
                pass

    def finalise(self, result: "AgentResult"):
        """Attach final structured output to the trace root and close root span."""
        if self._root_span:
            try:
                self._root_span.set_trace_io(
                    output={
                        "severity":          result.severity,
                        "root_cause":        result.root_cause,
                        "healing_performed": result.healing_performed,
                        "tool_calls_made":   result.tool_calls_made,
                        "fallback":          result.fallback,
                    }
                )
                self._root_span.update(
                    output={
                        "severity":   result.severity,
                        "root_cause": result.root_cause,
                        "healing":    result.healing_performed,
                        "tool_calls": result.tool_calls_made,
                    }
                )
            except Exception as exc:
                log.debug("Langfuse finalise failed (non-fatal): %s", exc)

    def __exit__(self, *_):
        # Close root span, then flush the queue to Langfuse Cloud
        if self._root_span:
            try:
                self._root_span.end()
            except Exception:
                pass
        lf = _langfuse_client
        if lf:
            try:
                lf.flush()
            except Exception:
                pass

# ── Rate limiter (shared quota with explainer.py) ───────────────────────────
_call_timestamps: deque = deque()
_rate_lock = threading.Lock()


def _check_rate_limit() -> bool:
    now = time.monotonic()
    with _rate_lock:
        cutoff = now - 3600
        while _call_timestamps and _call_timestamps[0] <= cutoff:
            _call_timestamps.popleft()
        if len(_call_timestamps) >= MAX_CLAUDE_CALLS_PER_HOUR:
            log.warning("Agent: rate limit reached (%d calls/hour)", MAX_CLAUDE_CALLS_PER_HOUR)
            return False
        _call_timestamps.append(now)
        return True


# ── Result dataclass (Tier-1 Step-2: structured output) ─────────────────────
@dataclass
class AgentResult:
    explanation: str                              # human-readable summary
    healing_performed: bool
    severity: str = "unknown"                     # low | medium | high | critical
    root_cause: str = ""
    recommended_action: str = ""
    actions_taken: list[str] = field(default_factory=list)
    tool_calls_made: int = 0
    fallback: bool = False                        # True when Claude was unavailable


# ── Tool schemas (Anthropic format) ─────────────────────────────────────────
TOOLS: list[dict] = [
    {
        "name": "query_prometheus",
        "description": (
            "Execute a PromQL query against the cluster Prometheus instance. "
            "Use to fetch current or historical metric values for any service. "
            "Prefer instant queries (lookback_minutes=0) for current state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {
                    "type": "string",
                    "description": "PromQL expression, e.g. rate(http_requests_total{job='victim-service'}[5m])",
                },
                "lookback_minutes": {
                    "type": "integer",
                    "description": "Range window in minutes. 0 = instant query.",
                    "default": 0,
                },
            },
            "required": ["promql"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Retrieve recent log lines from pods matching a label selector. "
            "Use to detect errors, stack traces, OOM kills, or restart loops."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace, e.g. 'app'",
                },
                "label_selector": {
                    "type": "string",
                    "description": "Label selector, e.g. 'app=victim-service'",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Number of recent log lines to return (default 50, max 200)",
                    "default": 50,
                },
            },
            "required": ["namespace", "label_selector"],
        },
    },
    {
        "name": "get_deployment_status",
        "description": (
            "Get current rollout status, replica counts, and last restart time for a deployment. "
            "Check this BEFORE deciding to restart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
            },
            "required": ["namespace", "deployment"],
        },
    },
    {
        "name": "restart_deployment",
        "description": (
            "Trigger a rolling restart of a Kubernetes deployment. "
            "Call ONLY when logs or metrics clearly evidence a degraded state. "
            "A cooldown prevents repeated restarts within 15 minutes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "One-sentence justification citing specific evidence",
                },
            },
            "required": ["namespace", "deployment", "reason"],
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

def _tool_query_prometheus(promql: str, lookback_minutes: int = 0) -> dict:
    try:
        if lookback_minutes > 0:
            url = f"{PROMETHEUS_URL}/api/v1/query_range"
            end = time.time()
            params = {
                "query": promql,
                "start": end - lookback_minutes * 60,
                "end": end,
                "step": "60s",
            }
        else:
            url = f"{PROMETHEUS_URL}/api/v1/query"
            params = {"query": promql}

        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            return {"error": data.get("error", "prometheus query failed")}

        # Cap result size — LLM context is not free
        results = data["data"]["result"][:10]
        return {"result": results, "result_count": len(results)}

    except requests.Timeout:
        return {"error": "prometheus query timed out (5s)"}
    except Exception as exc:
        return {"error": str(exc)}


def _tool_get_pod_logs(
    namespace: str,
    label_selector: str,
    tail_lines: int = 50,
) -> dict:
    tail_lines = min(tail_lines, 200)  # hard cap
    try:
        from kubernetes import client as k8s, config as k8s_cfg

        try:
            k8s_cfg.load_incluster_config()
        except Exception:
            k8s_cfg.load_kube_config()

        v1 = k8s.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

        if not pods.items:
            return {"error": f"No pods found for selector '{label_selector}' in {namespace}"}

        pod_name = pods.items[0].metadata.name
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )
        # Cap at ~3 KB to stay within context budget
        return {"pod": pod_name, "logs": logs[-3000:], "truncated": len(logs) > 3000}

    except Exception as exc:
        return {"error": str(exc)}


def _tool_get_deployment_status(namespace: str, deployment: str) -> dict:
    try:
        from kubernetes import client as k8s, config as k8s_cfg

        try:
            k8s_cfg.load_incluster_config()
        except Exception:
            k8s_cfg.load_kube_config()

        apps = k8s.AppsV1Api()
        d = apps.read_namespaced_deployment(name=deployment, namespace=namespace)
        annotations = d.spec.template.metadata.annotations or {}

        return {
            "desired_replicas":   d.spec.replicas,
            "ready_replicas":     d.status.ready_replicas or 0,
            "available_replicas": d.status.available_replicas or 0,
            "last_restart":       annotations.get("kubectl.kubernetes.io/restartedAt", "never"),
            "conditions": [
                {"type": c.type, "status": c.status, "message": c.message}
                for c in (d.status.conditions or [])
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}


def _tool_restart_deployment(
    namespace: str,
    deployment: str,
    reason: str,
    incident_id: str = "",
) -> dict:
    # Delegate to existing healer — preserves cooldown, Prometheus metrics, locking
    from healer import rollout_restart

    log.info("[%s] Agent requesting restart — reason: %s", incident_id, reason)
    success = rollout_restart(
        namespace=namespace,
        deployment=deployment,
        incident_id=incident_id,
    )
    return {"success": success, "reason": reason}


# ── Tool dispatcher ──────────────────────────────────────────────────────────

def _execute_tool(tool_name: str, tool_input: dict, incident_id: str) -> str:
    """Route a tool call to its implementation and return JSON string result."""
    agent_tool_calls.labels(tool_name=tool_name).inc()
    log.info("[%s] → tool=%s args=%s", incident_id, tool_name, list(tool_input.keys()))

    if tool_name == "query_prometheus":
        result = _tool_query_prometheus(**tool_input)
    elif tool_name == "get_pod_logs":
        result = _tool_get_pod_logs(**tool_input)
    elif tool_name == "get_deployment_status":
        result = _tool_get_deployment_status(**tool_input)
    elif tool_name == "restart_deployment":
        result = _tool_restart_deployment(**tool_input, incident_id=incident_id)
    else:
        result = {"error": f"Unknown tool: {tool_name}"}

    return json.dumps(result, default=str)


# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM = """\
You are an autonomous SRE agent with tool access to a Kubernetes cluster running in production.

When given an anomaly alert your workflow is:
1. Call query_prometheus to get deeper metric context (error rates, latency percentiles, saturation).
2. Call get_pod_logs to check for errors, OOM events, or stack traces.
3. Call get_deployment_status to understand the current rollout state.
4. Only call restart_deployment if evidence clearly supports it — log errors, crash loops,
   or sustained metric degradation. If metrics are borderline, do NOT restart.

Finish your response with a JSON object (raw, no markdown fences) structured exactly as:
{
  "root_cause": "one sentence",
  "severity": "low|medium|high|critical",
  "recommended_action": "one sentence",
  "summary": "2-3 sentence explanation for the on-call engineer"
}
"""


# ── Main entry point ─────────────────────────────────────────────────────────

def run_agent(
    score: float,
    threshold: float,
    metrics: dict[str, float],
    baseline: dict[str, float],
    incident_id: str = "",
) -> AgentResult:
    """
    Run agentic anomaly analysis.
    Claude calls tools to gather evidence, then decides whether to heal.
    Falls back to a rule-based AgentResult if Claude is unavailable.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("[%s] ANTHROPIC_API_KEY not set — agent fallback", incident_id)
        return _fallback_result(score, metrics, baseline)

    if not _check_rate_limit():
        return _fallback_result(score, metrics, baseline)

    agent_runs_total.inc()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    initial_user_message = (
        f"Anomaly detected by Isolation Forest model.\n"
        f"Score: {score:.4f}  (threshold: {threshold:.4f} — lower is more anomalous)\n\n"
        f"Metric snapshot vs baseline:\n"
        f"  error_rate:   {metrics.get('error_rate',   0.0):.3f}  "
        f"(baseline {baseline.get('error_rate',   0.0):.3f})\n"
        f"  request_rate: {metrics.get('request_rate', 0.0):.3f}  "
        f"(baseline {baseline.get('request_rate', 0.0):.3f})\n"
        f"  p95_latency:  {metrics.get('p95_latency',  0.0):.3f}s "
        f"(baseline {baseline.get('p95_latency',  0.0):.3f}s)\n"
        f"  cpu_usage:    {metrics.get('cpu_usage',    0.0):.4f}  "
        f"(baseline {baseline.get('cpu_usage',    0.0):.4f})\n"
        f"  memory:       {metrics.get('memory_usage', 0.0) / 1024 / 1024:.1f} MB  "
        f"(baseline {baseline.get('memory_usage', 0.0) / 1024 / 1024:.1f} MB)\n\n"
        f"Target: namespace={TARGET_NAMESPACE}, deployment={TARGET_DEPLOYMENT}\n\n"
        f"Investigate using the available tools and remediate if warranted."
    )

    messages: list[dict] = [{"role": "user", "content": initial_user_message}]
    actions_taken: list[str] = []
    healing_performed = False
    tool_calls_made = 0
    t_start = time.monotonic()

    try:
        with _Tracer(incident_id, score, metrics) as tracer:
            for iteration in range(MAX_AGENT_ITERATIONS):
                iter_label = f"iteration-{iteration + 1}"
                log.info("[%s] Agent %s/%d", incident_id, iter_label, MAX_AGENT_ITERATIONS)

                iter_span = tracer.span(
                    iter_label,
                    input_data={"message_count": len(messages), "tool_calls_so_far": tool_calls_made},
                )

                response = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=1024,
                    system=_SYSTEM,
                    tools=TOOLS,
                    messages=messages,
                )

                # ── Token accounting ─────────────────────────────────────────
                usage = getattr(response, "usage", None)
                if usage:
                    in_tok  = getattr(usage, "input_tokens",  0)
                    out_tok = getattr(usage, "output_tokens", 0)
                    agent_input_tokens.inc(in_tok)
                    agent_output_tokens.inc(out_tok)
                    token_usage = {"input": in_tok, "output": out_tok}
                else:
                    token_usage = None

                # ── Record generation in Langfuse ────────────────────────────
                final_text_for_gen = next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )
                tracer.generation(
                    name        = iter_label,
                    model       = CLAUDE_MODEL,
                    input_msgs  = messages,
                    output_text = final_text_for_gen,
                    usage       = token_usage,
                    metadata    = {
                        "stop_reason":    response.stop_reason,
                        "tool_calls_turn": sum(
                            1 for b in response.content if getattr(b, "type", "") == "tool_use"
                        ),
                    },
                )

                # Append full assistant turn to history
                messages.append({"role": "assistant", "content": response.content})

                # ── Done reasoning ───────────────────────────────────────────
                if response.stop_reason == "end_turn":
                    elapsed = time.monotonic() - t_start
                    agent_latency.observe(elapsed)

                    log.info(
                        "[%s] Agent complete: %d iter, %d tool calls, %.2fs",
                        incident_id, iteration + 1, tool_calls_made, elapsed,
                    )
                    result = _parse_result(
                        final_text_for_gen, actions_taken, healing_performed, tool_calls_made
                    )
                    tracer.end_span(iter_span, output={"stop_reason": "end_turn"})
                    tracer.finalise(result)
                    return result

                # ── Unexpected stop ──────────────────────────────────────────
                if response.stop_reason != "tool_use":
                    log.warning("[%s] Unexpected stop_reason: %s", incident_id, response.stop_reason)
                    tracer.end_span(iter_span, output={"stop_reason": response.stop_reason})
                    break

                # ── Execute all tool calls in this turn ──────────────────────
                tool_results: list[dict] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    tool_calls_made += 1
                    result_json = _execute_tool(block.name, block.input, incident_id)

                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_json,
                    })

                    short_args = json.dumps(block.input, default=str)[:80]
                    actions_taken.append(f"{block.name}({short_args})")

                    if block.name == "restart_deployment":
                        try:
                            if json.loads(result_json).get("success"):
                                healing_performed = True
                        except json.JSONDecodeError:
                            pass

                tracer.end_span(
                    iter_span,
                    output={
                        "tool_calls_this_turn": len(tool_results),
                        "tools_called": [
                            b.name for b in response.content
                            if getattr(b, "type", "") == "tool_use"
                        ],
                    },
                )
                messages.append({"role": "user", "content": tool_results})

            # MAX_AGENT_ITERATIONS exceeded
            log.warning("[%s] Agent hit max iterations (%d)", incident_id, MAX_AGENT_ITERATIONS)
            result = _fallback_result(score, metrics, baseline, actions_taken, tool_calls_made)
            tracer.finalise(result)
            return result

    except Exception as exc:
        agent_errors_total.labels(reason=type(exc).__name__).inc()
        log.error("[%s] Agent error: %s", incident_id, exc)
        return _fallback_result(score, metrics, baseline, actions_taken, tool_calls_made)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_result(
    text: str,
    actions_taken: list[str],
    healing_performed: bool,
    tool_calls_made: int,
) -> AgentResult:
    """Extract structured fields from Claude's final message."""
    # Find last {...} block — Claude appends it after prose
    try:
        last_close = text.rfind("}")
        last_open  = text.rfind("{", 0, last_close + 1)
        if last_open >= 0:
            data = json.loads(text[last_open : last_close + 1])
            return AgentResult(
                explanation       = data.get("summary", text[:500]),
                healing_performed = healing_performed,
                severity          = data.get("severity", "unknown"),
                root_cause        = data.get("root_cause", ""),
                recommended_action= data.get("recommended_action", ""),
                actions_taken     = actions_taken,
                tool_calls_made   = tool_calls_made,
            )
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: use raw text without structured fields
    return AgentResult(
        explanation       = text[:600],
        healing_performed = healing_performed,
        actions_taken     = actions_taken,
        tool_calls_made   = tool_calls_made,
    )


def _fallback_result(
    score: float,
    metrics: dict[str, float],
    baseline: dict[str, float],
    actions_taken: list[str] | None = None,
    tool_calls_made: int = 0,
) -> AgentResult:
    worst = max(
        ["error_rate", "p95_latency"],
        key=lambda m: abs(metrics.get(m, 0) - baseline.get(m, 0))
        / max(baseline.get(m, 0.001), 0.001),
    )
    return AgentResult(
        explanation=(
            f"Anomaly detected (score={score:.3f}). "
            f"Most deviated: {worst}={metrics.get(worst, 0):.3f} "
            f"(baseline={baseline.get(worst, 0):.3f}). "
            f"Investigate victim-service logs."
        ),
        healing_performed=False,
        severity="unknown",
        actions_taken=actions_taken or [],
        tool_calls_made=tool_calls_made,
        fallback=True,
    )
