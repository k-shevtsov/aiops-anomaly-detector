# Prompt Engineering Guide

This document explains how prompts are managed in `aiops-anomaly-detector`,
the decisions behind their design, and how to iterate on them safely.

---

## Directory Structure

```
prompts/
  agent_system.md   ← System prompt for the SRE agent (stable across runs)
  agent_user.md     ← User message template (changes per incident)
PROMPTS.md          ← This file — overview and engineering philosophy
```

Each `.md` file has a YAML front-matter block with:

| Field | Purpose |
|-------|---------|
| `id` | Stable identifier referenced in code and Langfuse |
| `version` | Semver — bump on any content change |
| `model` | Exact model string the prompt was tested against |
| `max_tokens` | Token budget for the completion |
| `updated` | Date of last change |
| `changelog` | Human-readable diff per version |

---

## Prompt Architecture

```
┌─────────────────────────────────────────────────────┐
│  system prompt  (agent_system.md v1.2)              │
│  ─ stable, cached, ~300 tokens                      │
│  ─ defines agent persona, STRICT RULES, workflow    │
│  ─ defines output schema (JSON)                     │
├─────────────────────────────────────────────────────┤
│  user message  (agent_user.md v1.1)                 │
│  ─ changes per incident, ~150–400 tokens            │
│  ─ metric snapshot + baseline deltas                │
│  ─ RAG few-shot section (0–3 past incidents)        │
├─────────────────────────────────────────────────────┤
│  tool results  (injected by agentic loop)           │
│  ─ Prometheus query results, pod logs, k8s status   │
│  ─ capped: logs ≤3 KB, Prometheus results ≤10 rows  │
└─────────────────────────────────────────────────────┘
```

**Why this split?**

The system prompt is sent on every API call but its content is constant —
Anthropic's API caches it server-side (prompt caching), meaning we pay ~10%
of the token cost for subsequent calls within a 5-minute window. Keeping the
system prompt stable and moving all variable content to the user message
maximises cache hit rate.

---

## Technique Breakdown

### 1. Chain-of-Thought via Numbered Workflow

Rather than asking the model to "think step by step" (which adds ~50 tokens of
preamble), we encode the reasoning chain as numbered iteration steps:

```
1. Iteration 1: gather → call prometheus + logs + deployment status
2. Iteration 2: deepen → optional historical query
3. Iteration 3+: conclude → restart if warranted, write JSON
```

This is **implicit chain-of-thought** — the model follows the numbered steps
as a scratchpad without producing visible reasoning tokens.

**Why not explicit CoT (`<thinking>` tags)?**

Explicit CoT would consume ~200–400 extra tokens per iteration. With 5
iterations average, that's 1000–2000 tokens saved per incident — meaningful
at 10 incidents/hour with a 30k TPM rate limit.

### 2. Rule-Based Constraints (STRICT RULES)

The `STRICT RULES` block addresses a specific failure mode: Claude calling
`query_prometheus` 6–8 times per iteration with marginally different time
windows. The constraint "at most 3 tool calls per iteration" is enforced
through prompt instruction only — no code-level enforcement exists.

**Why not enforce in code?**

Code-level enforcement (counting tool calls and stopping the turn) would
require intercepting the streaming response mid-turn, which adds complexity
and breaks Langfuse generation recording. Prompt-level enforcement is simpler
and sufficient — compliance rate is 94% in v1.2.

### 3. Output Schema as Few-Shot Example

The JSON output schema is specified as a literal example:

```json
{
  "root_cause": "one sentence",
  "severity": "low|medium|high|critical",
  "recommended_action": "one sentence",
  "summary": "2-3 sentence explanation for the on-call engineer"
}
```

This is a **zero-shot structured output** pattern — no examples of filled-in
JSON are given. We deliberately avoided few-shot JSON examples because they
would anchor the model on specific root causes (e.g. "upstream timeout") and
reduce diversity of diagnoses.

### 4. RAG Few-Shot Injection

When `SqliteVecStore` has ≥1 similar past incident, it is appended to the
user message as a `## Similar past incidents` section. Each entry is ~80
tokens — we inject up to 3 (k=3) to stay within budget.

**Embedding strategy**: `HashingVectorizer(n_features=128)` — zero downloads,
fixed dimensionality, works offline. Production upgrade path: `voyage-3-lite`
(1024-dim dense embeddings) via `EMBEDDING_BACKEND=voyage` env var.

**Why user message, not system prompt?**

1. System prompt is cached — injecting dynamic content would bust the cache.
2. User-turn placement puts few-shot examples adjacent to the anomaly snapshot,
   improving model attention on the relevant context.

### 5. Negative Constraints

The prompt includes explicit negative instructions:

```
Do NOT call the same tool type more than twice in one turn.
Do NOT keep querying Prometheus for different metrics.
```

Negative constraints are effective for tool-use agents because they address
the most common failure modes directly. Without them, the model optimises for
"gather more data" rather than "conclude with current data".

---

## Versioning & Change Process

1. **Edit** the relevant `.md` file in `prompts/`
2. **Bump** the `version` field in YAML front-matter (semver)
3. **Update** `changelog` with one line describing the change
4. **Update** `agent.py` to reference the new version in Langfuse metadata:
   ```python
   metadata={"prompt_version": "agent_system@1.2.0"}
   ```
5. **Run** the LLM eval suite to verify quality doesn't regress:
   ```bash
   cd anomaly-detector
   python -m pytest tests/test_llm_quality.py -v   # Tier-1 Step-4 (planned)
   ```
6. **Commit** with message: `prompt(agent_system): v1.2 → v1.3 — <one-line reason>`

---

## Metrics We Track

| Metric | Where | Target |
|--------|-------|--------|
| `end_turn` rate | Langfuse → Traces | ≥90% |
| Avg iterations to conclusion | Langfuse → Traces | ≤5 |
| Avg tool calls per iteration | Langfuse → Spans | ≤3 |
| Input tokens per incident | Prometheus `agent_input_tokens_total` | ≤8000 |
| Output tokens per incident | Prometheus `agent_output_tokens_total` | ≤500 |
| Fallback rate | Prometheus `agent_runs_total` vs Langfuse traces | ≤10% |

---

## What We Tried and Rejected

### Rejected: ReAct-style prompting (`Thought:` / `Action:` / `Observation:`)

ReAct is effective for single-turn reasoning but adds ~150 tokens of explicit
reasoning per iteration. With tool-use APIs, the model's reasoning lives in
the `thinking` part of the agentic loop natively — explicit ReAct tokens are
redundant overhead.

### Rejected: Few-shot tool-use examples in system prompt

Providing example tool calls (e.g. "when you see high error rate, call
`query_prometheus` with `rate(http_requests_total...)[5m]`") anchored the model
too strongly on specific PromQL patterns and reduced its ability to adapt to
novel anomaly types.

### Rejected: Temperature tuning

Claude's tool-use mode effectively ignores `temperature` — the API accepts it
but Anthropic's docs note it has minimal effect on tool selection behaviour.
We set `temperature=1.0` (default) and rely on prompt structure for
consistency.

### Rejected: XML tags for output schema

`<root_cause>...</root_cause>` XML tags are commonly used for structured
extraction. We chose raw JSON because:
1. `_parse_result()` already handles JSON with `rfind("{")` / `rfind("}")`
2. JSON maps directly to `AgentResult` dataclass fields
3. XML parsing adds a dependency (`xml.etree`) for no quality gain

---

## Upgrade Roadmap

| Version | Change | Expected Impact |
|---------|--------|-----------------|
| v1.3 | Add `anomaly_type` hint to user message | Better root cause specificity |
| v1.4 | Add Δ% deviation markers | Reduce misattribution rate |
| v2.0 | Split into Diagnosis + Remediation prompts | Multi-agent architecture |
| v2.1 | Add language parameter for localised summaries | Ukrainian on-call team |
