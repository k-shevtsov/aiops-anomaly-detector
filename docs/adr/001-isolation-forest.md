# ADR-001: Anomaly Detection Algorithm Selection

**Date:** 2026-03-29
**Status:** Accepted

## Context

The aiops-anomaly-detector requires an ML algorithm to detect anomalies in
Kubernetes service metrics (error rate, request rate, p95 latency, CPU, memory)
before Alertmanager fires. The algorithm must work without labeled training data,
since we cannot know in advance which metric combinations constitute an anomaly.

## Decision

We use **Isolation Forest** (scikit-learn) as the anomaly detection algorithm.

## Considered Alternatives

### Z-score / Statistical threshold
- Pros: simple, interpretable, no training required
- Cons: assumes normal distribution, requires per-metric tuning, cannot capture
  correlations between metrics (e.g. high error rate + low request rate together)
- Verdict: rejected — too many false positives in noisy microservice environments

### Prophet (Facebook)
- Pros: handles seasonality and trends well
- Cons: requires historical time-series data (days/weeks), not suitable for
  short baseline collection (10 minutes), high memory footprint
- Verdict: rejected — does not fit our short baseline requirement

### LSTM (Long Short-Term Memory)
- Pros: captures temporal dependencies, state-of-the-art for time series
- Cons: requires large labeled dataset, GPU for training, complex deployment,
  not interpretable, overkill for 5-feature input
- Verdict: rejected — operational complexity outweighs benefits for this scope

### Isolation Forest ✅
- Pros:
  - Unsupervised — no labeled data required
  - Works well with small baseline (20+ samples)
  - Handles multi-dimensional feature space natively
  - Fast inference (~1ms per prediction)
  - Interpretable anomaly score (-1 to 0 scale)
  - Dynamic threshold via score_samples() quantile
- Cons:
  - Does not capture temporal ordering of metrics
  - Sensitive to baseline quality (garbage in → garbage out)
  - contamination='auto' may not suit all traffic patterns

## Implementation Details

- **Training:** minimum 20 samples AND 600 seconds of normal traffic
- **Features:** [error_rate, request_rate, p95_latency, cpu_usage, memory_usage]
- **Threshold:** dynamic — 2nd percentile of baseline scores (np.quantile(scores, 0.02))
- **Contamination:** 'auto' — sklearn determines expected anomaly fraction
- **Buffer:** deque(maxlen=2000) — prevents unbounded memory growth

## Consequences

- System requires a clean baseline period before inference begins
- Pod restarts reset the model — no persistence across restarts (tech debt)
- False positives possible when baseline includes degraded traffic
- Multi-replica deployment would require shared model storage (out of scope)
