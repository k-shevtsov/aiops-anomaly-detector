# MCP Server Setup — AIOps Anomaly Detector

The anomaly detector exposes a **Model Context Protocol (MCP) server** that
lets any MCP client — Claude Desktop, Claude.ai, or your own tool — query the
live Kubernetes cluster directly in conversation.

## What you can ask Claude after connecting

> "What's the current anomaly score for victim-service?"
> "Show me the last 3 incidents and their root causes."
> "Query Prometheus for the p95 latency of victim-service right now."
> "Trigger an immediate analysis of the cluster."
> "Check the pod logs for any errors in the app namespace."

---

## Option A — Claude Desktop (STDIO, local)

This is the easiest setup. The MCP server runs as a subprocess of Claude Desktop.

### 1. Install the MCP dependency in your venv

```bash
cd ~/aiops-anomaly-detector/anomaly-detector
source venv/bin/activate
pip install "mcp[cli]"
echo 'mcp[cli]>=1.2.0' >> requirements.txt
```

### 2. Start port-forwards (so the server can reach Prometheus)

```bash
cd ~/aiops-anomaly-detector
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090 &
kubectl port-forward -n ai-engine  svc/anomaly-detector 8001:8001 &
```

### 3. Add to Claude Desktop config

Open (create if missing):
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "aiops-anomaly-detector": {
      "command": "/home/kostiantyn/aiops-anomaly-detector/anomaly-detector/venv/bin/python",
      "args": [
        "/home/kostiantyn/aiops-anomaly-detector/anomaly-detector/src/mcp_server.py"
      ],
      "env": {
        "PROMETHEUS_URL": "http://localhost:9090",
        "DETECTOR_URL":   "http://localhost:8001",
        "SQLITE_DB_PATH": "/home/kostiantyn/aiops-anomaly-detector/data/incidents.db"
      }
    }
  }
}
```

> **Note**: adjust paths to match your actual venv location.
> Run `which python` inside the activated venv to get the exact path.

### 4. Restart Claude Desktop

The MCP server icon (🔌) appears in the toolbar when connected.
Type `@aiops-anomaly-detector` to invoke it, or just ask naturally.

---

## Option B — HTTP transport (remote / Claude.ai)

Useful when the cluster runs on a remote machine or you want to expose
the MCP server to multiple clients.

### 1. Run the server in HTTP mode

```bash
cd ~/aiops-anomaly-detector/anomaly-detector
source venv/bin/activate

PROMETHEUS_URL=http://localhost:9090 \
DETECTOR_URL=http://localhost:8001 \
SQLITE_DB_PATH=/data/incidents.db \
python src/mcp_server.py --http --port 8002
```

The server starts at `http://0.0.0.0:8002/mcp`.

### 2. Test with MCP Inspector

```bash
# In a separate terminal:
npx @modelcontextprotocol/inspector http://localhost:8002/mcp
```

Opens a browser UI where you can call tools interactively.

### 3. Deploy as a Kubernetes service (optional)

Add to `infra/helm/anomaly-detector/templates/deployment.yaml`:

```yaml
- name: mcp-server
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  command: ["python", "src/mcp_server.py", "--http", "--port", "8002"]
  ports:
    - containerPort: 8002
  env:
    - name: PROMETHEUS_URL
      value: "http://monitoring-kube-prometheus-prometheus.monitoring:9090"
    - name: DETECTOR_URL
      value: "http://localhost:8001"
    - name: SQLITE_DB_PATH
      value: "/data/incidents.db"
```

---

## Available Tools

| Tool | Description |
|------|-------------|
| `get_anomaly_status()` | Current score, phase, threshold, baseline |
| `get_recent_incidents(n)` | Last N incidents from RAG store |
| `get_prometheus_metric(promql)` | Run any PromQL query |
| `get_pod_logs(namespace, selector)` | Live pod logs |
| `trigger_manual_analysis(reason)` | Force agent analysis right now |

## Available Resources

| Resource URI | Description |
|-------------|-------------|
| `detector://status` | Live detector status JSON |
| `detector://incidents` | Last 10 incidents JSON |
| `detector://prometheus-targets` | Prometheus scrape targets |

## Available Prompts

| Prompt | Description |
|--------|-------------|
| `investigate_anomaly(incident_id?)` | Pre-built investigation workflow |

---

## Troubleshooting

**"No module named 'mcp'"**
```bash
pip install "mcp[cli]" --break-system-packages
```

**"Connection refused on 9090"**
```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090 &
```

**"No incidents stored yet"**
Incidents are saved only after the agent completes a successful `end_turn`.
Trigger chaos, wait for a detection cycle, then query again.

**MCP server not appearing in Claude Desktop**
Check the config file path — it differs by OS. Restart Claude Desktop after
editing. Check logs at `~/Library/Logs/Claude/` (macOS).
