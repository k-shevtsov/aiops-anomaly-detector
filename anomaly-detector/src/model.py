import numpy as np
from collections import deque
from sklearn.ensemble import IsolationForest
from datetime import datetime, timezone
from prometheus_client import Gauge, Counter

anomaly_score_gauge = Gauge(
    'anomaly_detector_score',
    'Isolation Forest anomaly score (lower = more anomalous)'
)
anomaly_detected_counter = Counter(
    'anomaly_detector_detected_total',
    'Total anomalies detected'
)

MAX_TRAINING_SAMPLES = 2000
ANOMALY_THRESHOLD = -0.5  # fallback, overridden by dynamic threshold after training


class AnomalyDetector:
    def __init__(self, min_training_samples: int = 20):
        self.min_training_samples = min_training_samples
        self.model = IsolationForest(
            n_estimators=100,
            contamination='auto',
            random_state=42
        )
        self.is_trained = False
        self.training_data: deque = deque(maxlen=MAX_TRAINING_SAMPLES)
        self.baseline: dict[str, float] = {}
        self.threshold: float = ANOMALY_THRESHOLD

    def add_training_sample(self, features: np.ndarray) -> None:
        if features.ndim != 2 or features.shape[1] != 5:
            raise ValueError("features must be shape (1, 5)")
        self.training_data.append(features)
        print(f"[{datetime.now(timezone.utc)}] Training sample #{len(self.training_data)} collected")

    def train(self) -> bool:
        if len(self.training_data) < self.min_training_samples:
            print(f"[{datetime.now(timezone.utc)}] Not enough samples: "
                  f"{len(self.training_data)}/{self.min_training_samples}")
            return False

        X = np.vstack(self.training_data)

        # Guard: IsolationForest degenerates on zero-variance data.
        # If all features are constant (e.g. all-zero baseline), inject
        # small synthetic noise so the model learns a meaningful boundary.
        feature_std = X.std(axis=0)
        if feature_std.max() < 1e-6:
            noise = np.random.default_rng(42).normal(0, 0.01, X.shape)
            X_train = X + noise
        else:
            X_train = X

        self.model.fit(X_train)
        self.is_trained = True
        scores = self.model.score_samples(X_train)
        self.threshold = float(np.quantile(scores, 0.05))  # 5th percentile

        self.baseline = {
            'error_rate':   float(X[:, 0].mean()),
            'request_rate': float(X[:, 1].mean()),
            'p95_latency':  float(X[:, 2].mean()),
            'cpu_usage':    float(X[:, 3].mean()),
            'memory_usage': float(X[:, 4].mean()),
        }
        print(f"[{datetime.now(timezone.utc)}] Model trained on {len(self.training_data)} samples")
        print(f"[{datetime.now(timezone.utc)}] Baseline: {self.baseline}")
        print(f"[{datetime.now(timezone.utc)}] Dynamic threshold: {self.threshold:.3f}")
        return True

    def predict(self, features: np.ndarray) -> tuple[float, bool]:
        if not self.is_trained:
            raise RuntimeError("Model is not trained yet")
        if features.ndim != 2 or features.shape[1] != 5:
            raise ValueError("features must be shape (1, 5)")
        score = float(self.model.score_samples(features)[0])
        is_anomaly = score < self.threshold
        anomaly_score_gauge.set(score)
        if is_anomaly:
            anomaly_detected_counter.inc()
        print(f"[{datetime.now(timezone.utc)}] score={score:.3f} "
              f"threshold={self.threshold:.3f} anomaly={is_anomaly}")
        return score, is_anomaly
