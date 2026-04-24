
import random
import time
import os
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, make_asgi_app
import uvicorn

app = FastAPI()

REQUEST_COUNT    = Counter("http_requests_total",             "Total requests",    ["status"])
REQUEST_LATENCY  = Histogram("http_request_duration_seconds", "Request latency")
CHAOS_MODE       = {"active": False}

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def root():
    start = time.time()
    if CHAOS_MODE["active"] and random.random() < 0.7:
        REQUEST_COUNT.labels(status="500").inc()
        REQUEST_LATENCY.observe(random.uniform(2.0, 5.0))
        return {"error": "chaos"}, 500
    REQUEST_COUNT.labels(status="200").inc()
    REQUEST_LATENCY.observe(time.time() - start)
    return {"status": "ok"}

@app.post("/chaos/start")
def chaos_start():
    CHAOS_MODE["active"] = True
    return {"chaos": "started"}

@app.post("/chaos/stop")
def chaos_stop():
    CHAOS_MODE["active"] = False
    return {"chaos": "stopped"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
