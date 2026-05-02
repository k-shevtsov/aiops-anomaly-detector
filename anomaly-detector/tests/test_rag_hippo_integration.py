"""
tests/test_rag_hippo_integration.py  —  Integration tests for HippoRAG backend.

Requires:
  - OPENAI_API_KEY env var
  - embedding-server port-forward: kubectl port-forward -n shared-infra svc/embedding-server-embedding-server 8001:8001

Run:
  pytest tests/test_rag_hippo_integration.py -v -s -m integration
Skip in CI:
  pytest tests/ -m "not integration"
"""

import os
import sys
import time
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytestmark = pytest.mark.integration


def _ollama_available() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/v1/models", timeout=3)
        return True
    except Exception:
        return False


def _openai_key_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", ""))


if not _openai_key_available():
    pytest.skip("OPENAI_API_KEY not set", allow_module_level=True)


def _make_incident(incident_id="inc-001", severity="high",
                   root_cause="error rate spike due to upstream timeout",
                   error_rate=1.5, p95_latency=3.2):
    from rag import Incident
    return Incident(
        incident_id        = incident_id,
        timestamp          = "2026-04-30T10:00:00+00:00",
        score              = -0.65,
        severity           = severity,
        root_cause         = root_cause,
        metrics_snapshot   = {
            "error_rate":   error_rate,
            "request_rate": 0.8,
            "p95_latency":  p95_latency,
            "cpu_usage":    0.3,
            "memory_usage": 40_000_000.0,
        },
        explanation        = f"Detected anomaly: {root_cause}",
        recommended_action = "Restart deployment and check upstream dependency",
        healing_performed  = True,
    )


@pytest.fixture(scope="function")
def fresh_store(tmp_path):
    """
    Fresh HippoRagStore per test — avoids state leakage between tests.
    Uses function scope so each test gets clean graph + cache.
    """
    os.environ["HIPPO_SAVE_DIR"]     = str(tmp_path)
    os.environ["HIPPO_LLM_MODEL"]    = "gpt-4o-mini"
    os.environ["HIPPO_EMBED_MODEL"]  = "facebook/contriever"

    from rag_hippo import HippoRagStore
    return HippoRagStore()


class TestHippoRagIntegration:

    def test_store_single_incident(self, fresh_store):
        """Indexing one incident must succeed and increment count."""
        assert fresh_store.count() == 0
        fresh_store.store(_make_incident("integ-001"))
        assert fresh_store.count() == 1

    def test_retrieve_returns_result(self, fresh_store):
        """After storing, retrieval must return at least one result."""
        fresh_store.store(_make_incident(
            "integ-002",
            root_cause="high memory usage OOM killed worker",
            error_rate=0.1, p95_latency=0.2,
        ))
        results = fresh_store.retrieve("memory OOM killed worker", k=3)
        assert isinstance(results, list)
        assert len(results) >= 1
        assert results[0].incident_id == "integ-002"

    def test_retrieve_semantic_ordering(self, fresh_store):
        """Most relevant incident should rank first."""
        fresh_store.store(_make_incident(
            "integ-error",
            root_cause="upstream service timeout causing error rate spike",
            error_rate=2.5, p95_latency=5.0,
        ))
        fresh_store.store(_make_incident(
            "integ-memory",
            severity="medium",
            root_cause="memory leak in worker process causing OOM",
            error_rate=0.01, p95_latency=0.1,
        ))

        results = fresh_store.retrieve(
            "high error rate latency timeout upstream service",
            k=2,
        )
        assert len(results) >= 1
        assert results[0].incident_id == "integ-error", (
            f"Expected integ-error first, got: {[r.incident_id for r in results]}"
        )

    def test_retrieve_as_few_shot_format(self, fresh_store):
        """Few-shot output must be formatted for Claude prompt injection."""
        fresh_store.store(_make_incident("integ-fs"))
        text = fresh_store.retrieve_as_few_shot(
            "error rate spike upstream timeout", k=1
        )
        if text:
            assert "similar past incident" in text
            assert "Root cause:" in text
            assert "Action:" in text

    def test_openie_cache_reuse(self, tmp_path):
        """
        Second index() on same docs should be faster (cache hit).
        Uses same save_dir so cache persists between two store instances.
        """
        os.environ["HIPPO_SAVE_DIR"]    = str(tmp_path)
        os.environ["HIPPO_LLM_MODEL"]   = "gpt-4o-mini"
        os.environ["HIPPO_EMBED_MODEL"] = "facebook/contriever"

        from rag_hippo import HippoRagStore
        inc = _make_incident("integ-cache")

        store1 = HippoRagStore()
        t1 = time.time()
        store1.store(inc)
        first_duration = time.time() - t1

        # Second store with same save_dir — should hit openie_cache
        store2 = HippoRagStore()
        t2 = time.time()
        store2.store(inc)
        second_duration = time.time() - t2

        print(f"\nFirst index: {first_duration:.2f}s, Cached: {second_duration:.2f}s")
        assert store1.count() >= 1
        # Cache should be meaningfully faster
        assert second_duration < first_duration * 0.7, (
            f"Cache not effective: first={first_duration:.2f}s, second={second_duration:.2f}s"
        )

    def test_retrieve_empty_query_does_not_crash(self, fresh_store):
        fresh_store.store(_make_incident("integ-eq"))
        results = fresh_store.retrieve("", k=3)
        assert isinstance(results, list)

    def test_upsert_updates_incident(self, fresh_store):
        """Storing same id twice must not duplicate."""
        fresh_store.store(_make_incident("integ-upsert", severity="low"))
        count_before = fresh_store.count()
        fresh_store.store(_make_incident("integ-upsert", severity="critical"))
        assert fresh_store.count() == count_before
        assert fresh_store._incidents["integ-upsert"].severity == "critical"
