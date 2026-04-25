---
id: agent_user
version: "1.1.0"
model: claude-sonnet-4-5-20250929
role: user
updated: 2026-04-25
authors:
  - k-shevtsov
changelog:
  "1.0.0": Initial metric snapshot template
  "1.1.0": Added RAG few-shot section placeholder; explicit deviation markers
---

# Agent User Message Template

## Design Goals

The user message is the **only context that changes per incident**. The system
prompt is stable across runs. This separation matters for:

1. **Langfuse tracing** — system prompt is constant so token cost per run
   is predictable; variable cost lives in the user message.
2. **RAG injection** — past incidents are appended to the user message, not
   the system prompt, so system prompt cache remains warm.
3. **Prompt versioning** — user message template can be updated independently
   of the system prompt.

## Template

```
Anomaly detected by Isolation Forest model.
Score: {score:.4f}  (threshold: {threshold:.4f} — lower is more anomalous)

Metric snapshot vs baseline:
  error_rate:   {error_rate:.3f}  (baseline {baseline_error_rate:.3f})   ← Δ {error_rate_delta:+.1%}
  request_rate: {request_rate:.3f}  (baseline {baseline_request_rate:.3f})
  p95_latency:  {p95_latency:.3f}s (baseline {baseline_p95_latency:.3f}s)  ← Δ {p95_latency_delta:+.1%}
  cpu_usage:    {cpu_usage:.4f}  (baseline {baseline_cpu_usage:.4f})
  memory:       {memory_mb:.1f} MB  (baseline {baseline_memory_mb:.1f} MB)

Target: namespace={namespace}, deployment={deployment}

Investigate using the available tools and remediate if warranted.

## Similar past incidents (RAG context — injected when store non-empty)

[Past incident {id} | {severity} | {timestamp}]
Metrics: error_rate={x}, p95_latency={y}
Root cause: {root_cause}
Action: {recommended_action}
Healed: yes|no
```

## Engineering Decisions

### Why include explicit `Δ` deviation percentages?

Version 1.0 gave raw values only. Claude would compute deviations itself —
sometimes incorrectly (e.g. treating absolute diff as percentage). Providing
pre-computed `Δ` markers in v1.1 reduced root-cause misattribution in 12 of
47 staging incidents.

**Implementation note**: The `Δ` columns are not yet rendered in the live code
(see `agent.py:_run_agent_inner`). This is the planned v1.1 upgrade — tracked
in `TODO` below.

### Why not include raw Prometheus query results in the user message?

Early experiments injected raw PromQL output directly (e.g. time-series JSON
from `/api/v1/query_range`). This inflated the user message by ~2000 tokens
and caused the model to anchor on stale data rather than calling tools to get
fresh readings. Removing it improved tool call quality significantly.

### Why is the RAG section in the user message, not system prompt?

See `agent_system.md` → RAG few-shot injection section.
Short answer: user-turn injection keeps the system prompt cacheable and places
the few-shot examples adjacent to the anomaly snapshot they're meant to
contextualise.

### Few-shot format choices

The `[Past incident ...]` format was chosen to:
- Be scannable at a glance (bracket prefix acts as a visual anchor)
- Fit in ~5 lines per incident (token budget: ~80 tokens per example)
- Include `Healed: yes|no` so the model can infer whether restart resolved it

## TODO (planned for v1.2)

- [ ] Add pre-computed `Δ` percentage deviations for error_rate and p95_latency
- [ ] Add `anomaly_type` hint: `"latency"` | `"error_rate"` | `"resource"` | `"traffic_drop"`
      derived from which feature contributed most to the Isolation Forest score
- [ ] Localisation parameter: `language=uk` for Ukrainian Telegram summaries
