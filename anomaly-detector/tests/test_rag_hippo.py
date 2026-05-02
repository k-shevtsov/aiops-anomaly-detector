"""
tests/test_rag_hippo.py  —  Unit tests for HippoRAG backend.

HippoRAG is fully mocked — no Ollama or embedding-server needed in CI.
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from unittest.mock import MagicMock

def _mock_query_solution(docs):
    """Create a mock QuerySolution matching HippoRAG's real return type."""
    qs = MagicMock()
    qs.docs = docs
    qs.doc_scores = [0.9 - i * 0.1 for i in range(len(docs))]
    return qs

# ensure src/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_incident(incident_id="inc-001", severity="high",
                   root_cause="error rate spike"):
    from rag import Incident
    return Incident(
        incident_id        = incident_id,
        timestamp          = "2026-04-30T10:00:00+00:00",
        score              = -0.65,
        severity           = severity,
        root_cause         = root_cause,
        metrics_snapshot   = {"error_rate": 1.5, "p95_latency": 3.2},
        explanation        = "High error rate detected",
        recommended_action = "Restart deployment",
        healing_performed  = True,
    )


def _make_store(tmp_path):
    """Create HippoRagStore with fully mocked HippoRAG internals."""
    mock_hippo = MagicMock()
    mock_hippo.index.return_value = None
    mock_hippo.retrieve.return_value = [[]]

    with patch.dict(os.environ, {"HIPPO_SAVE_DIR": str(tmp_path)}), \
         patch("hipporag.HippoRAG", return_value=mock_hippo):
        from rag_hippo import HippoRagStore
        store = HippoRagStore()
        store._hippo = mock_hippo
    return store, mock_hippo


class TestHippoRagStore:

    def test_empty_store_count_is_zero(self, tmp_path):
        store, _ = _make_store(tmp_path)
        assert store.count() == 0

    def test_store_increments_count(self, tmp_path):
        store, _ = _make_store(tmp_path)
        store.store(_make_incident("i1"))
        assert store.count() == 1
        store.store(_make_incident("i2"))
        assert store.count() == 2

    def test_store_calls_hippo_index(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        store.store(_make_incident("i1"))
        mock_hippo.index.assert_called_once()
        docs = mock_hippo.index.call_args[1]["docs"]
        assert any("i1" in doc for doc in docs)

    def test_upsert_same_incident_id(self, tmp_path):
        store, _ = _make_store(tmp_path)
        store.store(_make_incident("dup", severity="low"))
        store.store(_make_incident("dup", severity="high"))
        assert store.count() == 1
        assert store._incidents["dup"].severity == "high"

    def test_retrieve_empty_returns_empty_list(self, tmp_path):
        store, _ = _make_store(tmp_path)
        assert store.retrieve("error rate spike") == []

    def test_retrieve_matches_by_incident_id_in_doc(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        store.store(_make_incident("inc-test-001"))
        mock_hippo.retrieve.return_value = [
            _mock_query_solution(["Incident inc-test-001 occurred at 2026-04-30"])
        ]
        results = store.retrieve("error rate high latency")
        assert len(results) == 1
        assert results[0].incident_id == "inc-test-001"

    def test_retrieve_respects_k_limit(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        for i in range(5):
            store.store(_make_incident(f"inc-{i:03d}"))
        mock_hippo.retrieve.return_value = [
            _mock_query_solution([f"Incident inc-{i:03d} occurred" for i in range(5)])
        ]
        results = store.retrieve("error", k=2)
        assert len(results) <= 2

    def test_retrieve_returns_empty_on_hippo_error(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        store.store(_make_incident("i1"))
        mock_hippo.retrieve.side_effect = Exception("HippoRAG error")
        assert store.retrieve("query") == []

    def test_retrieve_as_few_shot_empty(self, tmp_path):
        store, _ = _make_store(tmp_path)
        assert store.retrieve_as_few_shot("anything") == ""

    def test_retrieve_as_few_shot_non_empty(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        store.store(_make_incident("inc-fs-001"))
        mock_hippo.retrieve.return_value = [
            _mock_query_solution(["Incident inc-fs-001 occurred"])
        ]
        text = store.retrieve_as_few_shot("error rate")
        assert "similar past incident" in text
        assert "inc-fs-0" in text  # to_few_shot_text truncates id to 8 chars

    def test_store_index_failure_does_not_raise(self, tmp_path):
        store, mock_hippo = _make_store(tmp_path)
        mock_hippo.index.side_effect = Exception("Index failed")
        store.store(_make_incident("i1"))  # must not raise
        assert store.count() == 1

    def test_incident_to_doc_contains_key_fields(self, tmp_path):
        store, _ = _make_store(tmp_path)
        inc = _make_incident("doc-test", root_cause="memory leak in worker")
        doc = store._incident_to_doc(inc)
        assert "doc-test" in doc
        assert "memory leak in worker" in doc
        assert "error_rate" in doc
        assert "Restart deployment" in doc


class TestHippoRagGetStore:

    def test_get_store_returns_hipporag_store(self, tmp_path):
        import rag
        original = rag._store_instance
        rag._store_instance = None

        mock_hippo = MagicMock()
        mock_hippo.index.return_value = None
        mock_hippo.retrieve.return_value = [[]]

        try:
            with patch.dict(os.environ, {
                "VECTOR_STORE":   "hipporag",
                "HIPPO_SAVE_DIR": str(tmp_path),
            }), patch("hipporag.HippoRAG", return_value=mock_hippo):
                import importlib
                importlib.reload(rag)
                store = rag.get_store()
                from rag_hippo import HippoRagStore
                assert isinstance(store, HippoRagStore)
        finally:
            rag._store_instance = original
