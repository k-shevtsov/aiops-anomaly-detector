"""
tests/test_rag.py  —  Unit tests for RAG incident store (Tier-2 Step-1).

Covers:
  1. Incident dataclass serialisation + few-shot text formatting
  2. SqliteVecStore store / retrieve / count lifecycle
  3. Similarity ordering — most relevant incident retrieved first
  4. Empty store returns empty list, not exception
  5. store_resolved_incident integrates AgentResult → Incident correctly
  6. get_store() singleton returns same instance across calls
  7. RAG injection in run_agent — few_shot text present in user message
  8. RAG no-op when store is empty (graceful, no exception)
"""

import json
import os
import sys
import tempfile
import types
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_incident(
    incident_id="inc-001",
    severity="high",
    root_cause="error rate spike due to upstream timeout",
    error_rate=1.5,
    p95_latency=3.2,
    healing=True,
    explanation="High error rate detected, upstream service returned 503s",
    recommended_action="Restart deployment and check upstream dependency",
) -> "Incident":
    from rag import Incident
    return Incident(
        incident_id        = incident_id,
        timestamp          = "2026-04-24T10:00:00+00:00",
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
        explanation        = explanation,
        recommended_action = recommended_action,
        healing_performed  = healing,
    )


def _make_store(tmp_path: str) -> "SqliteVecStore":
    """Create a fresh SqliteVecStore in a temp directory."""
    from rag import SqliteVecStore
    return SqliteVecStore(db_path=os.path.join(tmp_path, "test.db"))


# ── Incident dataclass ────────────────────────────────────────────────────────

class TestIncidentDataclass:
    def test_to_few_shot_text_contains_key_fields(self):
        inc = _make_incident()
        text = inc.to_few_shot_text()
        assert "inc-001" in text
        assert "HIGH" in text
        assert "error rate spike" in text
        assert "Restart deployment" in text
        assert "yes" in text   # healing_performed=True

    def test_to_few_shot_text_healing_false(self):
        inc = _make_incident(healing=False)
        assert "no" in inc.to_few_shot_text()

    def test_serialise_roundtrip(self):
        inc  = _make_incident()
        data = asdict(inc)
        from rag import Incident
        inc2 = Incident(**data)
        assert inc2.incident_id == inc.incident_id
        assert inc2.severity    == inc.severity
        assert inc2.root_cause  == inc.root_cause


# ── SqliteVecStore ────────────────────────────────────────────────────────────

class TestSqliteVecStore:
    def test_empty_store_count_is_zero(self, tmp_path):
        store = _make_store(str(tmp_path))
        assert store.count() == 0

    def test_store_increments_count(self, tmp_path):
        store = _make_store(str(tmp_path))
        store.store(_make_incident("i1"))
        assert store.count() == 1
        store.store(_make_incident("i2", severity="low"))
        assert store.count() == 2

    def test_retrieve_empty_store_returns_empty_list(self, tmp_path):
        store  = _make_store(str(tmp_path))
        result = store.retrieve("error rate spike", k=3)
        assert result == []

    def test_retrieve_returns_incident(self, tmp_path):
        store = _make_store(str(tmp_path))
        inc   = _make_incident("i1")
        store.store(inc)
        results = store.retrieve("error rate spike upstream timeout", k=3)
        assert len(results) >= 1
        assert results[0].incident_id == "i1"

    def test_retrieve_similarity_ordering(self, tmp_path):
        """Most semantically similar incident should come first."""
        store = _make_store(str(tmp_path))
        # Store two incidents — one matches query well, one is unrelated
        store.store(_make_incident(
            "i-error",
            root_cause="error rate spike due to upstream timeout",
            error_rate=1.5, p95_latency=3.2,
        ))
        store.store(_make_incident(
            "i-memory",
            severity="medium",
            root_cause="memory leak in worker threads causing OOM",
            error_rate=0.0, p95_latency=0.1,
            explanation="Memory usage grew steadily over 30 minutes",
            recommended_action="Restart to reclaim memory",
        ))
        results = store.retrieve("error rate high latency timeout", k=2)
        assert len(results) >= 1
        # The error-rate incident should rank higher than memory incident
        assert results[0].incident_id == "i-error"

    def test_retrieve_respects_k_limit(self, tmp_path):
        store = _make_store(str(tmp_path))
        for i in range(5):
            store.store(_make_incident(f"i{i}", error_rate=float(i)))
        results = store.retrieve("error rate spike", k=2)
        assert len(results) <= 2

    def test_upsert_same_incident_id(self, tmp_path):
        """Storing same incident_id twice should not duplicate."""
        store = _make_store(str(tmp_path))
        store.store(_make_incident("dup", severity="low"))
        store.store(_make_incident("dup", severity="high"))   # same id, updated
        assert store.count() == 1
        # Use a descriptive query that matches the incident's embed_text
        results = store.retrieve("error rate spike upstream timeout high latency", k=1)
        assert len(results) == 1
        assert results[0].severity == "high"

    def test_retrieve_as_few_shot_empty(self, tmp_path):
        store = _make_store(str(tmp_path))
        text  = store.retrieve_as_few_shot("anything", k=3)
        assert text == ""

    def test_retrieve_as_few_shot_non_empty(self, tmp_path):
        store = _make_store(str(tmp_path))
        store.store(_make_incident("i1"))
        text = store.retrieve_as_few_shot("error rate spike", k=3)
        # Should contain the past incident header
        assert "similar past incident" in text
        assert "[Past incident" in text


# ── store_resolved_incident ───────────────────────────────────────────────────

class TestStoreResolvedIncident:
    def test_stores_agent_result_correctly(self, tmp_path):
        from rag import store_resolved_incident, get_store, SqliteVecStore, _store_lock
        import rag

        # Inject a fresh store pointing to tmp_path
        fresh_store = SqliteVecStore(db_path=str(tmp_path / "resolved.db"))
        original    = rag._store_instance

        rag._store_instance = fresh_store
        try:
            mock_result = MagicMock()
            mock_result.severity           = "high"
            mock_result.root_cause         = "upstream timeout"
            mock_result.explanation        = "p95 latency exceeded 4s"
            mock_result.recommended_action = "restart deployment"
            mock_result.healing_performed  = True

            store_resolved_incident(
                result      = mock_result,
                score       = -0.65,
                metrics     = {"error_rate": 1.2, "p95_latency": 4.1},
                incident_id = "res-001",
            )

            assert fresh_store.count() == 1
            results = fresh_store.retrieve("latency error", k=1)
            assert len(results) == 1
            assert results[0].incident_id == "res-001"
            assert results[0].severity    == "high"
            assert results[0].healing_performed is True
        finally:
            rag._store_instance = original

    def test_store_resolved_incident_no_exception_on_error(self):
        """store_resolved_incident must never raise — it's called in critical path."""
        from rag import store_resolved_incident
        import rag

        original = rag._store_instance
        rag._store_instance = None   # force get_store() to re-init with default path

        bad_result = MagicMock()
        bad_result.severity           = None    # deliberately bad data
        bad_result.root_cause         = None
        bad_result.explanation        = None
        bad_result.recommended_action = None
        bad_result.healing_performed  = None

        try:
            # Should not raise regardless of bad input
            store_resolved_incident(bad_result, 0.0, {}, "bad-001")
        except Exception as exc:
            pytest.fail(f"store_resolved_incident raised unexpectedly: {exc}")
        finally:
            rag._store_instance = original


# ── get_store singleton ───────────────────────────────────────────────────────

class TestGetStoreSingleton:
    def test_returns_same_instance(self, tmp_path):
        import rag
        from rag import SqliteVecStore

        original = rag._store_instance
        rag._store_instance = None  # reset singleton

        try:
            with patch.dict(os.environ, {
                "VECTOR_STORE":   "sqlite",
                "SQLITE_DB_PATH": str(tmp_path / "singleton.db"),
            }):
                # Re-import to pick up env
                import importlib
                importlib.reload(rag)
                s1 = rag.get_store()
                s2 = rag.get_store()
                assert s1 is s2
        finally:
            rag._store_instance = original

    def test_unknown_backend_raises(self):
        import rag
        original = rag._store_instance
        rag._store_instance = None
        try:
            with patch.dict(os.environ, {"VECTOR_STORE": "nonexistent"}):
                import importlib
                importlib.reload(rag)
                with pytest.raises(ValueError, match="Unknown VECTOR_STORE"):
                    rag.get_store()
        finally:
            rag._store_instance = original


# ── RAG injection in agent.py ─────────────────────────────────────────────────

class TestAgentRagInjection:
    """Verify that run_agent injects few-shot context when store has incidents."""

    def _load_agent(self):
        sys.modules.pop("agent", None)
        for dep in ("kubernetes", "requests"):
            sys.modules.setdefault(dep, types.ModuleType(dep))
        fake_lf = types.ModuleType("langfuse")
        fake_lf.Langfuse = MagicMock(return_value=None)
        sys.modules["langfuse"] = fake_lf
        prom = types.ModuleType("prometheus_client")
        for cls_name in ("Counter", "Histogram", "Gauge"):
            mock_cls = MagicMock()
            mock_cls.return_value = MagicMock(
                inc=MagicMock(), observe=MagicMock(),
                labels=MagicMock(return_value=MagicMock(inc=MagicMock())),
            )
            setattr(prom, cls_name, mock_cls)
        sys.modules["prometheus_client"] = prom

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY":   "sk-test",
            "LANGFUSE_PUBLIC_KEY": "",
        }):
            import agent as ag
            return ag

    def test_rag_few_shot_injected_into_user_message(self, tmp_path):
        from rag import SqliteVecStore
        import rag as rag_mod

        # Seed the store with one incident
        store = SqliteVecStore(db_path=str(tmp_path / "agent_test.db"))
        store.store(_make_incident("seed-001"))
        original = rag_mod._store_instance
        rag_mod._store_instance = store

        ag = self._load_agent()

        captured_messages = []

        def mock_create(**kwargs):
            # Capture the messages sent to Claude
            captured_messages.extend(kwargs.get("messages", []))
            resp             = MagicMock()
            resp.stop_reason = "end_turn"
            block            = MagicMock()
            block.type       = "text"
            block.text       = '{"root_cause":"test","severity":"low","recommended_action":"none","summary":"ok"}'
            resp.content     = [block]
            resp.usage       = MagicMock(input_tokens=100, output_tokens=50)
            return resp

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_create

        try:
            with patch("anthropic.Anthropic", return_value=mock_client), \
                 patch("agent._langfuse_client", None), \
                 patch("agent._rag_enabled", True), \
                 patch("agent.get_store", return_value=store), \
                 patch("agent.store_resolved_incident"):
                ag.run_agent(
                    score       = -0.65,
                    threshold   = -0.50,
                    metrics     = {"error_rate": 1.2, "p95_latency": 3.1},
                    baseline    = {"error_rate": 0.0, "p95_latency": 0.1},
                    incident_id = "agent-rag-001",
                )
        finally:
            rag_mod._store_instance = original

        # The first user message should contain few-shot context
        assert len(captured_messages) > 0
        first_msg_content = captured_messages[0]["content"]
        assert "similar past incident" in first_msg_content or \
               "Past incident" in first_msg_content, \
               f"Expected RAG context in message, got: {first_msg_content[:200]}"

    def test_rag_disabled_no_exception(self, tmp_path):
        """When _rag_enabled=False, run_agent must complete without error."""
        ag = self._load_agent()

        resp             = MagicMock()
        resp.stop_reason = "end_turn"
        block            = MagicMock()
        block.type       = "text"
        block.text       = '{"root_cause":"x","severity":"low","recommended_action":"y","summary":"z"}'
        resp.content     = [block]
        resp.usage       = MagicMock(input_tokens=50, output_tokens=20)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch("agent._langfuse_client", None), \
             patch("agent._rag_enabled", False):
            result = ag.run_agent(
                score=0.1, threshold=0.5,
                metrics={"error_rate": 0.5},
                baseline={"error_rate": 0.0},
                incident_id="no-rag-001",
            )
        assert result is not None
        assert result.fallback is False
