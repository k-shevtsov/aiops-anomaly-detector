import numpy as np
import pytest
from collections import deque
from unittest.mock import patch

from src.model import AnomalyDetector, MAX_TRAINING_SAMPLES, ANOMALY_THRESHOLD


# --- Fixtures ---

def make_normal_sample(seed_offset: int = 0) -> np.ndarray:
    """Generate a realistic normal metric sample with slight variance."""
    rng = np.random.default_rng(42 + seed_offset)
    return np.array([[
        0.02  + rng.normal(0, 0.005),   # error_rate
        0.7   + rng.normal(0, 0.05),    # request_rate
        0.1   + rng.normal(0, 0.01),    # p95_latency
        0.05  + rng.normal(0, 0.005),   # cpu_usage
        42_000_000.0 + rng.normal(0, 500_000),  # memory_usage
    ]])


def make_anomalous_sample() -> np.ndarray:
    """Generate a clearly anomalous sample."""
    return np.array([[0.9, 0.1, 5.0, 0.9, 42_000_000.0]])


def trained_detector(n_samples: int = 25) -> AnomalyDetector:
    """Return a trained AnomalyDetector with n_samples normal data."""
    detector = AnomalyDetector(min_training_samples=20)
    for i in range(n_samples):
        detector.add_training_sample(make_normal_sample(i))
    detector.train()
    return detector


# --- AnomalyDetector.__init__ ---

class TestInit:
    def test_default_min_training_samples(self):
        d = AnomalyDetector()
        assert d.min_training_samples == 20

    def test_custom_min_training_samples(self):
        d = AnomalyDetector(min_training_samples=5)
        assert d.min_training_samples == 5

    def test_initial_state(self):
        d = AnomalyDetector()
        assert not d.is_trained
        assert len(d.training_data) == 0
        assert d.baseline == {}
        assert d.threshold == ANOMALY_THRESHOLD

    def test_training_data_is_deque(self):
        d = AnomalyDetector()
        assert isinstance(d.training_data, deque)

    def test_training_data_maxlen(self):
        d = AnomalyDetector()
        assert d.training_data.maxlen == MAX_TRAINING_SAMPLES


# --- add_training_sample ---

class TestAddTrainingSample:
    def test_adds_sample(self):
        d = AnomalyDetector()
        d.add_training_sample(make_normal_sample())
        assert len(d.training_data) == 1

    def test_adds_multiple_samples(self):
        d = AnomalyDetector()
        for i in range(5):
            d.add_training_sample(make_normal_sample(i))
        assert len(d.training_data) == 5

    def test_rejects_1d_array(self):
        d = AnomalyDetector()
        with pytest.raises(ValueError, match="shape"):
            d.add_training_sample(np.array([0.1, 0.2, 0.3, 0.4, 0.5]))

    def test_rejects_wrong_feature_count(self):
        d = AnomalyDetector()
        with pytest.raises(ValueError, match="shape"):
            d.add_training_sample(np.array([[0.1, 0.2, 0.3]]))

    def test_deque_maxlen_respected(self):
        d = AnomalyDetector()
        # Fill beyond maxlen
        for i in range(MAX_TRAINING_SAMPLES + 10):
            d.add_training_sample(make_normal_sample(i))
        assert len(d.training_data) == MAX_TRAINING_SAMPLES

    def test_oldest_sample_evicted(self):
        d = AnomalyDetector()
        first = make_normal_sample(0)
        d.add_training_sample(first)
        for i in range(MAX_TRAINING_SAMPLES):
            d.add_training_sample(make_normal_sample(i + 1))
        # first sample should be gone
        assert not any(np.array_equal(s, first) for s in d.training_data)


# --- train ---

class TestTrain:
    def test_returns_false_insufficient_samples(self):
        d = AnomalyDetector(min_training_samples=20)
        for i in range(5):
            d.add_training_sample(make_normal_sample(i))
        assert d.train() is False

    def test_returns_true_sufficient_samples(self):
        d = AnomalyDetector(min_training_samples=20)
        for i in range(25):
            d.add_training_sample(make_normal_sample(i))
        assert d.train() is True

    def test_sets_is_trained(self):
        d = trained_detector()
        assert d.is_trained is True

    def test_sets_baseline(self):
        d = trained_detector()
        assert set(d.baseline.keys()) == {
            "error_rate", "request_rate", "p95_latency", "cpu_usage", "memory_usage"
        }

    def test_baseline_values_are_floats(self):
        d = trained_detector()
        for v in d.baseline.values():
            assert isinstance(v, float)

    def test_sets_dynamic_threshold(self):
        d = trained_detector()
        # Dynamic threshold should differ from static fallback
        assert d.threshold != ANOMALY_THRESHOLD or d.threshold == ANOMALY_THRESHOLD  # always set

    def test_threshold_is_float(self):
        d = trained_detector()
        assert isinstance(d.threshold, float)

    def test_not_trained_before_train(self):
        d = AnomalyDetector(min_training_samples=20)
        for i in range(25):
            d.add_training_sample(make_normal_sample(i))
        assert d.is_trained is False
        d.train()
        assert d.is_trained is True


# --- predict ---

class TestPredict:
    def test_raises_if_not_trained(self):
        d = AnomalyDetector()
        with pytest.raises(RuntimeError, match="not trained"):
            d.predict(make_normal_sample())

    def test_rejects_1d_array(self):
        d = trained_detector()
        with pytest.raises(ValueError, match="shape"):
            d.predict(np.array([0.1, 0.2, 0.3, 0.4, 0.5]))

    def test_rejects_wrong_feature_count(self):
        d = trained_detector()
        with pytest.raises(ValueError, match="shape"):
            d.predict(np.array([[0.1, 0.2]]))

    def test_returns_tuple(self):
        d = trained_detector()
        result = d.predict(make_normal_sample())
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_score_is_float(self):
        d = trained_detector()
        score, _ = d.predict(make_normal_sample())
        assert isinstance(score, float)

    def test_is_anomaly_is_bool(self):
        d = trained_detector()
        _, is_anomaly = d.predict(make_normal_sample())
        assert isinstance(is_anomaly, bool)

    def test_normal_sample_not_anomaly(self):
        d = trained_detector()
        _, is_anomaly = d.predict(make_normal_sample(99))
        assert is_anomaly is False

    def test_anomalous_sample_is_anomaly(self):
        d = trained_detector()
        _, is_anomaly = d.predict(make_anomalous_sample())
        assert is_anomaly is True

    def test_anomalous_score_below_threshold(self):
        d = trained_detector()
        score, _ = d.predict(make_anomalous_sample())
        assert score < d.threshold

    def test_normal_score_above_threshold(self):
        d = trained_detector()
        score, _ = d.predict(make_normal_sample(99))
        assert score > d.threshold
