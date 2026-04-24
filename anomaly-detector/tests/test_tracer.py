"""
tests/test_tracer.py  —  Unit tests for _Tracer (Tier-1 Step-3: LLM Observability).

Covers:
  1. _Tracer is a pure no-op when _langfuse_client is None
  2. _Tracer delegates to Langfuse v4 API (start_observation / usage_details)
  3. Token Prometheus counters increment correctly from API response usage
  4. run_agent returns a fallback AgentResult when ANTHROPIC_API_KEY is absent

Patching strategy:
  _Tracer reads _langfuse_client as a module-level global at *call time* (lazy).
  The correct way to inject a mock is:  patch('agent._langfuse_client', client)
  which directly replaces the name in the module's __dict__ for the duration.
"""

import sys
import types
from unittest.mock import MagicMock, patch
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_response(
    stop_reason="end_turn",
    text='summary\n{"root_cause":"test","severity":"low","recommended_action":"none","summary":"ok"}',
    in_tokens=100,
    out_tokens=50,
):
    block      = MagicMock()
    block.type = "text"
    block.text = text

    usage               = MagicMock()
    usage.input_tokens  = in_tokens
    usage.output_tokens = out_tokens

    resp             = MagicMock()
    resp.stop_reason = stop_reason
    resp.content     = [block]
    resp.usage       = usage
    return resp


def _load_agent(anthropic_key: str = "sk-test") -> types.ModuleType:
    """
    Fresh import of agent with all heavy deps stubbed.
    _langfuse_client is always None after import (no LANGFUSE_PUBLIC_KEY).
    Tests inject a real mock via patch('agent._langfuse_client', client).
    """
    sys.modules.pop("agent", None)

    for dep in ("kubernetes", "requests"):
        sys.modules.setdefault(dep, types.ModuleType(dep))

    # Stub langfuse — no key means _langfuse_client = None
    fake_lf = types.ModuleType("langfuse")
    fake_lf.Langfuse = MagicMock(return_value=None)
    sys.modules["langfuse"] = fake_lf

    # Stub prometheus_client
    prom = types.ModuleType("prometheus_client")
    for cls_name in ("Counter", "Histogram", "Gauge"):
        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock(
            inc=MagicMock(),
            observe=MagicMock(),
            labels=MagicMock(return_value=MagicMock(inc=MagicMock())),
        )
        setattr(prom, cls_name, mock_cls)
    sys.modules["prometheus_client"] = prom

    with patch.dict("os.environ", {
        "ANTHROPIC_API_KEY":   anthropic_key,
        "LANGFUSE_PUBLIC_KEY": "",
    }):
        import agent as ag
        return ag


def _make_lf_client():
    root_span  = MagicMock(name="root_span")
    child_span = MagicMock(name="child_span")
    root_span.start_observation.return_value = child_span

    client = MagicMock(name="lf_client")
    client.start_observation.return_value = root_span
    client.create_trace_id.return_value   = "trace-abc123"
    return client, root_span, child_span


# ── No-op tests ───────────────────────────────────────────────────────────────

class TestTracerNoOp:
    def test_context_manager_no_exception(self):
        ag = _load_agent()
        with ag._Tracer("inc-001", 0.3, {"error_rate": 0.5}) as t:
            span = t.span("iter-1", {"x": 1})
            assert span is None
            t.generation("iter-1", "model", [], "text", usage={"input": 10, "output": 5})
            t.end_span(span)

    def test_finalise_no_exception_when_no_client(self):
        ag     = _load_agent()
        result = ag.AgentResult(explanation="x", healing_performed=False)
        with ag._Tracer("inc-002", 0.1, {}) as t:
            t.finalise(result)


# ── Langfuse v4 delegation tests ──────────────────────────────────────────────

class TestTracerWithClient:
    def test_root_agent_span_created_on_enter(self):
        ag = _load_agent()
        client, root_span, _ = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-003", 0.2, {"error_rate": 0.1}):
                pass

        client.start_observation.assert_called_once()
        _, kwargs = client.start_observation.call_args
        assert kwargs["name"]    == "anomaly-agent"
        assert kwargs["as_type"] == "agent"
        assert kwargs["input"]["score"] == pytest.approx(0.2)

    def test_trace_id_seeded_from_incident_id(self):
        ag = _load_agent()
        client, _, _ = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("my-incident-42", 0.5, {}):
                pass

        client.create_trace_id.assert_called_once_with(seed="my-incident-42")

    def test_generation_recorded_as_child_of_root_span(self):
        ag = _load_agent()
        client, root_span, child_span = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-004", 0.5, {}) as t:
                t.generation(
                    name        = "iter-1",
                    model       = "claude-test",
                    input_msgs  = [{"role": "user", "content": "hi"}],
                    output_text = "response",
                    usage       = {"input": 20, "output": 10},
                )

        root_span.start_observation.assert_called()
        _, kwargs = root_span.start_observation.call_args
        assert kwargs["as_type"]       == "generation"
        assert kwargs["model"]         == "claude-test"
        assert kwargs["usage_details"] == {"input": 20, "output": 10}
        child_span.end.assert_called()

    def test_span_lifecycle_open_and_close(self):
        ag = _load_agent()
        client, root_span, child_span = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-005", 0.4, {}) as t:
                span = t.span("iter-1", {"a": 1})
                assert span is child_span
                t.end_span(span, output={"stop_reason": "end_turn"})

        child_span.update.assert_called_with(output={"stop_reason": "end_turn"})
        child_span.end.assert_called()

    def test_flush_called_on_context_exit(self):
        ag = _load_agent()
        client, _, _ = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-006", 0.3, {}):
                pass

        client.flush.assert_called_once()

    def test_root_span_closed_on_context_exit(self):
        ag = _load_agent()
        client, root_span, _ = _make_lf_client()

        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-008", 0.3, {}):
                pass

        root_span.end.assert_called()

    def test_finalise_sets_trace_io_and_span_output(self):
        ag = _load_agent()
        client, root_span, _ = _make_lf_client()

        result = ag.AgentResult(
            explanation="x", healing_performed=True,
            severity="high", root_cause="oom", tool_calls_made=3,
        )
        with patch("agent._langfuse_client", client):
            with ag._Tracer("inc-007", 0.2, {}) as t:
                t.finalise(result)

        root_span.set_trace_io.assert_called_once()
        _, kw = root_span.set_trace_io.call_args
        assert kw["output"]["severity"]          == "high"
        assert kw["output"]["healing_performed"] is True
        assert kw["output"]["tool_calls_made"]   == 3

        update_outputs = [c.kwargs.get("output", {}) for c in root_span.update.call_args_list]
        tool_output = next((o for o in update_outputs if "tool_calls" in o), None)
        assert tool_output is not None, "No update() call with 'tool_calls' found"
        assert tool_output["tool_calls"] == 3


# ── Token counter tests ───────────────────────────────────────────────────────

class TestTokenCounters:
    def test_token_counters_increment_on_successful_response(self):
        ag            = _load_agent(anthropic_key="sk-test")
        mock_response = _make_response(in_tokens=123, out_tokens=45)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch("agent._langfuse_client", None):
            ag.run_agent(
                score       = 0.1,
                threshold   = 0.5,
                metrics     = {"error_rate": 0.9},
                baseline    = {"error_rate": 0.1},
                incident_id = "tok-001",
            )

        input_calls  = [c.args[0] for c in ag.agent_input_tokens.inc.call_args_list  if c.args]
        output_calls = [c.args[0] for c in ag.agent_output_tokens.inc.call_args_list if c.args]
        assert 123 in input_calls,  f"Expected 123 in input calls,  got {input_calls}"
        assert 45  in output_calls, f"Expected 45  in output calls, got {output_calls}"


# ── Fallback tests ────────────────────────────────────────────────────────────

class TestRunAgentFallback:
    def test_fallback_result_when_no_api_key(self):
        ag = _load_agent(anthropic_key="")

        result = ag.run_agent(
            score       = 0.2,
            threshold   = 0.5,
            metrics     = {"error_rate": 0.8, "p95_latency": 0.05},
            baseline    = {"error_rate": 0.01, "p95_latency": 0.02},
            incident_id = "fb-001",
        )

        assert result.fallback          is True
        assert result.healing_performed is False
        assert result.tool_calls_made   == 0
        assert "error_rate" in result.explanation or "p95_latency" in result.explanation
