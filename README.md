# aiops-anomaly-detector

> **AIOps Self-Healing Kubernetes platform** — Isolation Forest anomaly detection
> with an autonomous Claude agent that investigates, diagnoses, and heals
> production incidents before Alertmanager fires.

![Tests](https://github.com/k-shevtsov/aiops-anomaly-detector/actions/workflows/ci.yaml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![Claude](https://img.shields.io/badge/Claude-Sonnet_4.5-orange)
![MCP](https://img.shields.io/badge/MCP-server-purple)
![Langfuse](https://img.shields.io/badge/LLMOps-Langfuse-green)

## Key Differentiator

Reacts **before** Alertmanager fires. Isolation Forest detects anomalies 2–3
minutes earlier by scoring metric combinations rather than threshold breaches.
When an anomaly is detected, an **autonomous Claude agent** investigates using
live Prometheus data and pod logs, then decides whether to trigger a rollout
restart — no human in the loop.

---

## AI Engineering Features

This project demonstrates production AI Engineering patterns beyond basic LLM
API calls:

| Feature | Implementation | Signal |
|---------|---------------|--------|
| **Agentic Tool Use** | Claude calls `query_prometheus`, `get_pod_logs`, `restart_deployment` autonomously | Agent architecture |
| **Structured Outputs** | `AgentResult` dataclass with severity, root_cause, recommended_action | Production LLM patterns |
| **LLM Observability** | Langfuse v4 tracing — per-iteration spans, token counters, prompt versioning | LLMOps |
| **RAG** | sqlite-vec incident store — similar past incidents injected as few-shot context | Retrieval-augmented generation |
| **Prompt Engineering** | Versioned prompts in `prompts/`, ablation table, upgrade roadmap | Systematic prompt design |
| **MCP Server** | Claude Desktop queries live k8s cluster via Model Context Protocol | Cutting-edge AI tooling |
| **LLM Eval** | Claude-as-judge test suite, fallback rate tracking | AI quality assurance |

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
│         query_prometheus ×2                 (sqlite-vec store)      │
│         get_pod_logs                        similar incidents        │
│         get_deployment_status               as few-shot examples    │
│         restart_deployment?                                         │
│                    │                                                │
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
```

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
| **RAG Store** | **sqlite-vec (upgrade path: pgvector + voyage-3-lite)** |
| **LLM Observability** | **Langfuse v4 — traces, spans, token costs** |
| **MCP Server** | **FastMCP — Claude Desktop integration** |
| Self-Healing | Kubernetes Python SDK |
| Notifications | Telegram |
| Deploy | Helm |
| Tests | pytest (110+ tests incl. LLM eval) |

---

## How It Works

### Detection
1. **Training phase** (~10 min): collects baseline metrics from Prometheus
2. **Inference phase**: scores every 30s using trained Isolation Forest
3. Detects anomalies **2–3 min before** Alertmanager threshold breaches

### Agentic Response
4. Claude agent starts — retrieves similar past incidents from RAG store
   and injects them as few-shot context
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
TELEGRAM_TOKEN       # notifications
TELEGRAM_CHAT_ID
LANGFUSE_PUBLIC_KEY  # LLM observability (free tier)
LANGFUSE_SECRET_KEY
```

### 1. Start cluster
```bash
./scripts/cluster-up.sh
# Builds victim-service image, creates k3d cluster,
# installs Prometheus + ArgoCD + Chaos Mesh via Helm
```

### 2. Create secrets
```bash
kubectl create secret generic anomaly-detector-secrets \
  --namespace ai-engine \
  --from-literal=anthropic-api-key=$ANTHROPIC_API_KEY \
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
./scripts/port-forwards.sh
# Prometheus → localhost:9090
# Grafana    → localhost:3000
# ArgoCD     → localhost:8080
```

### 5. Run tests
```bash
cd anomaly-detector && source venv/bin/activate
python -m pytest tests/ -v
# 110+ tests including RAG store, LLM tracer, agent injection
```

### 6. Connect Claude Desktop (MCP)
```bash
# Run MCP server locally
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
| `VECTOR_STORE` | sqlite | `sqlite` or `pgvector` |
| `EMBEDDING_BACKEND` | hashing | `hashing`, `voyage`, `openai` |
| `RAG_TOP_K` | 3 | Few-shot examples from RAG |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse observability |
| `SQLITE_DB_PATH` | /data/incidents.db | RAG store path |

---

## Repository

[github.com/k-shevtsov/aiops-anomaly-detector](https://github.com/k-shevtsov/aiops-anomaly-detector)
