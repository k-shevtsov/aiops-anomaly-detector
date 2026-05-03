# aiops-anomaly-detector

> **AIOps Self-Healing Kubernetes platform** — Isolation Forest anomaly detection
> with an autonomous Claude agent that investigates, diagnoses, and heals
> production incidents before Alertmanager fires.

![Tests](https://github.com/k-shevtsov/aiops-anomaly-detector/actions/workflows/ci.yaml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![Claude](https://img.shields.io/badge/Claude-Sonnet_4.5-orange)
![MCP](https://img.shields.io/badge/MCP-server-purple)
![Langfuse](https://img.shields.io/badge/LLMOps-Langfuse-green)
![HippoRAG](https://img.shields.io/badge/RAG-HippoRAG-blueviolet)

## Key Differentiator

Reacts **before** Alertmanager fires. Isolation Forest detects anomalies 2–3
minutes earlier by scoring metric combinations rather than threshold breaches.
When an anomaly is detected, an **autonomous Claude agent** investigates using
live Prometheus data and pod logs, then decides whether to trigger a rollout
restart — no human in the loop.

The RAG layer learns from every resolved incident: **multi-hop knowledge graph
retrieval** (HippoRAG) surfaces root causes that span multiple documents,
enabling the agent to connect "high latency" → "postgres replica lag" →
"disk I/O saturation" across past incidents.

---

## AI Engineering Features

This project demonstrates production AI Engineering patterns beyond basic LLM
API calls:

| Feature | Implementation | Signal |
|---------|---------------|--------|
| **Agentic Tool Use** | Claude calls `query_prometheus`, `get_pod_logs`, `restart_deployment` autonomously | Agent architecture |
| **Structured Outputs** | `AgentResult` dataclass with severity, root_cause, recommended_action | Production LLM patterns |
| **LLM Observability** | Langfuse v4 tracing — per-iteration spans, token counters, prompt versioning | LLMOps |
| **Multi-hop RAG** | HippoRAG knowledge graph — PPR retrieval across linked incident facts | Advanced RAG |
| **RAG Store (default)** | sqlite-vec — similar past incidents as few-shot context, zero infra | Retrieval-augmented generation |
| **Cost-aware AI** | Local facebook/contriever embeddings + OpenAI only for OpenIE (cached) | Production cost design |
| **Prompt Engineering** | Versioned prompts in `prompts/`, ablation table, upgrade roadmap | Systematic prompt design |
| **MCP Server** | Claude Desktop queries live k8s cluster via Model Context Protocol | Cutting-edge AI tooling |
| **LLM Eval** | Claude-as-judge test suite, fallback rate tracking | AI quality assurance |
| **Shared Infra** | Reusable embedding-server Helm chart serving multiple projects | Platform Engineering |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Claude Desktop / claude.ai                   │
│                     (MCP client — natural language)                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ MCP (STDIO / HTTP)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     mcp_server.py (FastMCP)                         │
│  get_anomaly_status · get_recent_incidents · get_prometheus_metric  │
│  get_pod_logs · trigger_manual_analysis                             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              anomaly-detector (FastAPI + asyncio)                   │
│                                                                     │
│  Prometheus → collector.py → Isolation Forest (model.py)           │
│                                    │                                │
│                            anomaly detected?                        │
│                                    │                                │
│                                    ▼                                │
│                    ┌─────── agent.py (Claude) ────────┐            │
│                    │                                   │            │
│              Tool Use loop                      RAG context         │
│         query_prometheus ×2              ┌─ sqlite-vec (default)   │
│         get_pod_logs                     └─ HippoRAG (knowledge    │
│         get_deployment_status               graph + PPR retrieval) │
│         restart_deployment?                 similar incidents as    │
│                    │                        few-shot examples       │
│                    ▼                                                │
│              AgentResult                                            │
│         severity · root_cause                                       │
│         healing_performed                                           │
│         recommended_action                                          │
│                    │                                                │
│         ┌──────────┼──────────┐                                    │
│         ▼          ▼          ▼                                     │
│     Langfuse   Telegram    healer.py                               │
│    (tracing)  (notify)  (k8s rollout)                              │
│                    │                                                │
│                    ▼                                                │
│              rag.py — persist incident for future retrieval         │
└─────────────────────────────────────────────────────────────────────┘

Shared Infrastructure (shared-infra k3d namespace)
┌─────────────────────────────────────────────────────────────────────┐
│  embedding-server (Helm chart)                                      │
│  OpenAI-compatible /v1/embeddings — facebook/contriever (local)    │
│  PVC-backed model cache — used by HippoRAG across projects         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## RAG Architecture

Two backends implement the same `IncidentStore` interface — swap via env var:

```
VECTOR_STORE=sqlite      # default — zero infra, runs in-pod
VECTOR_STORE=hipporag    # knowledge graph + multi-hop PPR retrieval
VECTOR_STORE=pgvector    # production upgrade path
```

### HippoRAG backend (`src/rag_hippo.py`)

Builds a knowledge graph from past incidents using OpenIE triplet extraction,
then uses Personalized PageRank to find causally connected facts across
multiple incident documents.

```
Incident docs → OpenIE (gpt-4o-mini) → triplets → knowledge graph
                                                         │
Query: "high latency timeout"                           PPR
                                                         │
Retrieved: incident chain across 3 docs:                ▼
  (high latency) → depends_on → (postgres replica)  ranked docs
  (postgres replica) → degraded_by → (disk I/O)     → Incidents
  (disk I/O) → resolved_by → (node drain + PVC migration)
```

**Cost design:** OpenIE runs once per document and results are cached in
`openie_cache/`. Second indexing: **0.00s** vs 7.27s first run.
Embeddings use `facebook/contriever` — fully local, zero API cost.

---

## Stack

| Component | Technology |
|-----------|------------|
| Cluster | k3d (local) |
| GitOps | ArgoCD |
| Metrics | Prometheus + Grafana |
| Chaos | Chaos Mesh |
| ML Detection | scikit-learn Isolation Forest |
| **Agentic AI** | **Claude Sonnet 4.5 — Tool Use loop** |
| **Multi-hop RAG** | **HippoRAG — knowledge graph + Personalized PageRank** |
| **RAG Store (default)** | **sqlite-vec (upgrade path: pgvector + voyage-3-lite)** |
| **Embedding Server** | **facebook/contriever via shared Helm chart** |
| **LLM Observability** | **Langfuse v4 — traces, spans, token costs** |
| **MCP Server** | **FastMCP — Claude Desktop integration** |
| Self-Healing | Kubernetes Python SDK |
| Notifications | Telegram |
| Deploy | Helm |
| Tests | pytest (134 tests incl. LLM eval + integration) |

---

## How It Works

### Detection
1. **Training phase** (~10 min): collects baseline metrics from Prometheus
2. **Inference phase**: scores every 30s using trained Isolation Forest
3. Detects anomalies **2–3 min before** Alertmanager threshold breaches

### Agentic Response
4. Claude agent starts — retrieves similar past incidents from RAG store
   and injects them as few-shot context (multi-hop if HippoRAG enabled)
5. Agent calls tools iteratively (max 5 iterations, ~25s):
   - `query_prometheus` — fetches error rates, latency percentiles
   - `get_pod_logs` — checks for crashes, OOM, stack traces
   - `get_deployment_status` — checks rollout state
   - `restart_deployment` — only if evidence supports it
6. Returns structured `AgentResult`: severity, root_cause, recommended_action
7. Persists incident to RAG store for future similar incidents
8. Sends enriched Telegram notification with severity emoji + agent reasoning
9. Langfuse records full trace: iterations, token cost, prompt version

### MCP Interface
10. Claude Desktop / claude.ai can query the live cluster in natural language:
    > *"What's the current anomaly score?"*
    > *"Show me the last 3 incidents and their root causes."*
    > *"Run a Prometheus query for p95 latency right now."*

---

## Prompt Engineering

Prompts are versioned artifacts in `prompts/` — see [`PROMPTS.md`](PROMPTS.md)
for the full design rationale, ablation table, and upgrade roadmap.

| Prompt version | End-turn rate | Avg iterations | Avg tool calls/iter |
|---------------|--------------|----------------|---------------------|
| v1.0 zero-shot | 31% | 7.2 | 6.4 |
| v1.1 + STRICT RULES | 68% | 5.1 | 3.8 |
| v1.2 + per-iter budget | **94%** | **4.1** | **2.9** |

---

## Quick Start

### Prerequisites
```bash
# Required
k3d, kubectl, helm, docker, python 3.12

# API keys needed
ANTHROPIC_API_KEY    # Claude API
OPENAI_API_KEY       # HippoRAG OpenIE (gpt-4o-mini, cached — minimal cost)
TELEGRAM_TOKEN       # notifications
TELEGRAM_CHAT_ID
LANGFUSE_PUBLIC_KEY  # LLM observability (free tier)
LANGFUSE_SECRET_KEY
```

### 1. Bootstrap everything (after reboot)
```bash
make up
# Starts Ollama, k3d clusters, deploys embedding-server Helm chart,
# runs smoke tests. One command to restore full dev environment.
```

### 2. Create secrets
```bash
kubectl create secret generic anomaly-detector-secrets \
  --namespace ai-engine \
  --from-literal=anthropic-api-key=$ANTHROPIC_API_KEY \
  --from-literal=openai-api-key=$OPENAI_API_KEY \
  --from-literal=telegram-token=$TELEGRAM_TOKEN \
  --from-literal=telegram-chat-id=$TELEGRAM_CHAT_ID \
  --from-literal=langfuse-public-key=$LANGFUSE_PUBLIC_KEY \
  --from-literal=langfuse-secret-key=$LANGFUSE_SECRET_KEY
```

### 3. Deploy
```bash
helm upgrade --install anomaly-detector infra/helm/anomaly-detector/ \
  --namespace ai-engine --create-namespace
```

### 4. Port-forwards
```bash
make port-forwards
# embedding-server → localhost:8001
# Prometheus       → localhost:9090
# Grafana          → localhost:3000
```

### 5. Run tests
```bash
cd anomaly-detector && source venv/bin/activate

# Unit tests (no external deps)
python -m pytest tests/ -m "not integration" -v
# 134 tests — RAG store, LLM tracer, agent injection, HippoRAG unit

# Integration tests (requires OPENAI_API_KEY + make port-forwards)
python -m pytest tests/ -m integration -v -s
# 7 tests — real HippoRAG indexing + retrieval
```

### 6. Enable HippoRAG backend
```bash
# Set in your .env or Helm values
VECTOR_STORE=hipporag
OPENAI_API_KEY=sk-...        # for OpenIE triplet extraction (cached)
HIPPO_SAVE_DIR=/data/hipporag
HIPPO_LLM_MODEL=gpt-4o-mini
HIPPO_EMBED_MODEL=facebook/contriever  # runs locally
```

### 7. Connect Claude Desktop (MCP)
```bash
PROMETHEUS_URL=http://localhost:9090 \
DETECTOR_URL=http://localhost:8001 \
python anomaly-detector/src/mcp_server.py --http --port 8002
```

Add to `~/.config/Claude/claude_desktop_config.json` — see
[`docs/mcp-setup.md`](docs/mcp-setup.md) for full setup.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_TRAINING_SAMPLES` | 20 | Samples before training |
| `MIN_TRAINING_SECONDS` | 600 | Baseline duration |
| `SCRAPE_INTERVAL_SECONDS` | 30 | Collection interval |
| `HEALING_ENABLED` | true | Enable self-healing |
| `COOLDOWN_SECONDS` | 900 | Cooldown between heals |
| `MAX_CLAUDE_CALLS_PER_HOUR` | 10 | Claude API rate limit |
| `MAX_AGENT_ITERATIONS` | 8 | Max agent iterations |
| `VECTOR_STORE` | sqlite | `sqlite` · `hipporag` · `pgvector` |
| `EMBEDDING_BACKEND` | hashing | `hashing` · `voyage` · `openai` |
| `RAG_TOP_K` | 3 | Few-shot examples from RAG |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse observability |
| `SQLITE_DB_PATH` | /data/incidents.db | Default RAG store path |
| `HIPPO_SAVE_DIR` | /data/hipporag | HippoRAG graph storage |
| `HIPPO_LLM_MODEL` | gpt-4o-mini | OpenIE model (results cached) |
| `HIPPO_EMBED_MODEL` | facebook/contriever | Local embedding model |

---

## Related

- [`shared-infra-charts`](https://github.com/k-shevtsov/shared-infra-charts) —
  Reusable Helm charts: OpenAI-compatible embedding-server
  (`facebook/contriever`, PVC-cached) shared across projects

---

## Repository

[github.com/k-shevtsov/aiops-anomaly-detector](https://github.com/k-shevtsov/aiops-anomaly-detector)
