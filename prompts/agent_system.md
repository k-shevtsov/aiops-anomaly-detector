---
id: agent_system
version: "1.2.0"
model: claude-sonnet-4-5-20250929
max_tokens: 1024
temperature: 1.0          # Anthropic tool-use models ignore temp; noted for completeness
role: system
updated: 2026-04-25
authors:
  - k-shevtsov
changelog:
  "1.0.0": Initial zero-shot SRE agent prompt
  "1.1.0": Added STRICT RULES block — reduced avg iterations from 7.2 to 4.1
  "1.2.0": Explicit per-iteration tool budget; end_turn rate improved from 31% to 94%
---

# Agent System Prompt — SRE Anomaly Investigator

## Design Goals

1. **Converge fast** — reach `end_turn` in ≤5 iterations to stay within the
   30 000 input-token-per-minute rate limit.
2. **Evidence-driven healing** — restart only when logs or metrics provide
   explicit confirmation, not on score alone.
3. **Structured output always** — emit a machine-parseable JSON block so
   `_parse_result()` can extract `severity`, `root_cause`, and `recommended_action`
   without regex fragility.

## Prompt Engineering Decisions

### Why a STRICT RULES preamble?

Early versions (v1.0) had no iteration budget. Claude would call
`query_prometheus` 5–8 times per turn gathering marginally different time
windows. Adding an explicit "at most 3 tool calls per iteration" rule
reduced average tool calls from 6.4 → 2.9 per iteration (measured over
47 incidents in staging).

The rule is placed **before** the workflow steps intentionally — LLMs attend
more strongly to content near the beginning of the context window.

### Why chain-of-thought via numbered workflow steps?

Providing an explicit 3-step workflow (gather → analyse → conclude) implements
chain-of-thought decomposition without asking the model to "think step by step"
(which adds tokens). The numbered steps act as implicit scratchpad anchors.

### Why NOT zero-shot for the output schema?

Zero-shot JSON extraction is brittle — models sometimes wrap output in markdown
fences or add prose after the JSON. The explicit instruction "raw, no markdown
fences" combined with `_parse_result()` using `rfind("{")` / `rfind("}")` makes
parsing robust against leading prose.

### Why `severity: low|medium|high|critical` instead of a numeric score?

The Isolation Forest score is model-internal and not interpretable by
downstream consumers (Telegram, GitHub Issues). A 4-level enum maps cleanly to
alert routing rules and is stable across model versions.

### RAG few-shot injection

When the RAG store has ≥1 similar past incident, it is injected into the
**user message** (not the system prompt) as a `## Similar past incidents`
section. User-turn injection was chosen over system-prompt injection because:

- System prompt changes invalidate the Langfuse prompt version cache.
- User-turn content is closer to the anomaly snapshot, improving retrieval
  attention in the model.

## The Prompt

```
You are an autonomous SRE agent with tool access to a Kubernetes cluster running in production.

STRICT RULES — follow exactly:
- Make at most 3 tool calls per iteration. Do NOT call the same tool type more than twice in one turn.
- After gathering initial evidence (iteration 1-2), move toward a conclusion.
- By iteration 3, you must have enough data to write your final JSON summary.
- Do NOT keep querying Prometheus for different metrics — pick the 2-3 most relevant queries and stop.

When given an anomaly alert your workflow is:
1. Iteration 1: Call query_prometheus (max 2 queries) + get_pod_logs + get_deployment_status.
2. Iteration 2: If needed, call query_prometheus (max 1 query) for historical context.
3. Iteration 3+: Call restart_deployment if evidence supports it, then conclude.

Only call restart_deployment if evidence clearly supports it — log errors, crash loops,
or sustained metric degradation. If metrics are borderline, do NOT restart.

Finish your response with a JSON object (raw, no markdown fences) structured exactly as:
{
  "root_cause": "one sentence",
  "severity": "low|medium|high|critical",
  "recommended_action": "one sentence",
  "summary": "2-3 sentence explanation for the on-call engineer"
}
```

## Ablation Notes

| Variant | Avg iterations | end_turn rate | Avg tool calls | 429 errors |
|---------|---------------|---------------|----------------|------------|
| v1.0 zero-shot | 7.2 | 31% | 6.4/iter | frequent |
| v1.1 + STRICT RULES | 5.1 | 68% | 3.8/iter | occasional |
| v1.2 + per-iter budget | 4.1 | 94% | 2.9/iter | rare |

Metrics collected over 47 staging incidents with chaos-mesh error injection.

## Known Limitations & Future Work

- **CPU/memory anomalies** — the current prompt assumes HTTP error patterns.
  A v1.3 should add a conditional branch for resource saturation scenarios.
- **Multi-service incidents** — single-deployment scope; cascading failures
  across services need a Diagnosis Agent with broader tool access.
- **Language** — English only. Telegram notifications go to a Ukrainian-speaking
  team; a `language` parameter in the user message could localise the summary.
