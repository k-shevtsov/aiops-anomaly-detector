# aiops-anomaly-detector

AIOps Self-Healing Kubernetes platform with Isolation Forest anomaly detection
and LLM-powered explanations via Claude API.

## Key Differentiator

Reacts **before** Alertmanager fires. Isolation Forest detects anomalies 2–3
minutes earlier by scoring metric combinations rather than threshold breaches.

## Architecture
```
Victim Service → Prometheus → anomaly-detector (Python/FastAPI)
                                      │
                          Isolation Forest (scikit-learn)
                                      │
                           anomaly score > threshold?
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                   ▼
              Claude API       Kubernetes SDK        Telegram
           (explanation)      (rollout restart)   (notification)
                    │                 │                   │
                    └─────────────────┴───────────────────┘
                                      │
                              GitHub Issue
                           (audit trail) [planned]
```

## Stack

| Component | Technology |
|-----------|------------|
| Cluster | k3d |
| GitOps | ArgoCD |
| Metrics | Prometheus + Grafana |
| Chaos | Chaos Mesh |
| ML Detection | scikit-learn Isolation Forest |
| LLM Explanation | Claude API (claude-sonnet-4-5) |
| Self-Healing | Kubernetes Python SDK |
| Notifications | Telegram |
| Deploy | Helm |
| Tests | pytest (92 tests) |

## How It Works

1. **Training phase** (10 min): collects baseline metrics from Prometheus
2. **Inference phase**: scores every 30s using trained Isolation Forest
3. **On anomaly**: Claude API explains root cause, Kubernetes SDK restarts
   the deployment, Telegram notifies with full context

## Quick Start

### Prerequisites
- k3d, kubectl, helm, docker
- Anthropic API key, Telegram bot token

### Local Development
```bash
# Start cluster
./scripts/cluster-up.sh

# Port-forwards
./scripts/port-forwards.sh

# Run detector locally (short training for dev)
cd anomaly-detector
source venv/bin/activate
MIN_TRAINING_SAMPLES=5 MIN_TRAINING_SECONDS=30 SCRAPE_INTERVAL_SECONDS=10 \
  python3 src/main.py
```

### Deploy to Kubernetes
```bash
# Create secrets
kubectl create secret generic anomaly-detector-secrets \
  --namespace ai-engine \
  --from-literal=anthropic-api-key=<key> \
  --from-literal=telegram-token=<token> \
  --from-literal=telegram-chat-id=<chat_id>

# Deploy via Helm
helm upgrade --install anomaly-detector infra/helm/anomaly-detector/ \
  --namespace ai-engine --create-namespace
```

### Run Tests
```bash
cd anomaly-detector
source venv/bin/activate
pytest tests/ -v
# 92 passed
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_TRAINING_SAMPLES` | 20 | Minimum samples before training |
| `MIN_TRAINING_SECONDS` | 600 | Minimum baseline duration |
| `SCRAPE_INTERVAL_SECONDS` | 30 | Metrics collection interval |
| `HEALING_ENABLED` | true | Enable/disable self-healing |
| `COOLDOWN_SECONDS` | 900 | Cooldown between healing actions |
| `MAX_CLAUDE_CALLS_PER_HOUR` | 10 | Claude API rate limit |


## Repository

[github.com/k-shevtsov/aiops-anomaly-detector](https://github.com/k-shevtsov/aiops-anomaly-detector)
