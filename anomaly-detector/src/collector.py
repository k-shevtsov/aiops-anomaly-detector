import os
import time
import requests
import numpy as np
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

session = requests.Session()

QUERIES = {
    "error_rate":   'rate(http_requests_total{job="victim-service",status="500"}[1m])',
    "request_rate": 'rate(http_requests_total{job="victim-service"}[1m])',
    "p95_latency":  'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="victim-service"}[1m])) by (le))',
    "cpu_usage":    'rate(container_cpu_usage_seconds_total{pod=~"victim-service.*", container!="POD"}[1m])',
    "memory_usage": 'container_memory_usage_bytes{pod=~"victim-service.*", container!="POD"}',
}

def fetch_metric(query: str) -> float:
    try:
        resp = session.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            print(f"[{datetime.now(timezone.utc)}] WARNING: empty result for query: {query}")
            return 0.0
        return float(result[0]["value"][1])
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] ERROR fetching metric: {e}")
        return 0.0

def collect_metrics() -> dict[str, float]:
    metrics = {}
    for name, query in QUERIES.items():
        try:
            metrics[name] = fetch_metric(query)
        except Exception as e:
            print(f"[collect_metrics] Unexpected error for {name}: {e}")
            metrics[name] = 0.0
    return metrics

def collect_features(m: dict[str, float]) -> np.ndarray:
    return np.array([[
        m["error_rate"],
        m["request_rate"],
        m["p95_latency"],
        m["cpu_usage"],
        m["memory_usage"],
    ]])

if __name__ == "__main__":
    print(f"[{datetime.now(timezone.utc)}] Starting collector, Prometheus: {PROMETHEUS_URL}")
    for i in range(5):
        metrics = collect_metrics()
        features = collect_features(metrics)
        print(f"[{datetime.now(timezone.utc)}] metrics={metrics}")
        print(f"[{datetime.now(timezone.utc)}] features={features}")
        time.sleep(10)
