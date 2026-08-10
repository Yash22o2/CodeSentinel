"""
tests/test_graph.py
===================
Integration tests for the full LangGraph review pipeline.

Strategy: mock the Groq LLM (_llm.invoke) at the module level so no real API
calls are made. Tests verify that the graph routes correctly, findings flow
through critic → aggregator, and ReviewMetadata is correctly assembled.

We also test the _route_from_planner helper and the aggregator node in isolation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.github.diff_parser import DiffChunk
from app.graph.builder import _route_from_planner, review_graph
from app.graph.nodes.aggregator import aggregator_node
from app.schemas import AgentPlan, Finding, FindingCategory, ReviewMetadata, Severity


# ── Shared fixtures / helpers ─────────────────────────────────────────────────


def _chunk(
    file_path: str = "app/main.py",
    added_lines: list[tuple[int, str]] | None = None,
) -> DiffChunk:
    added = added_lines or [(1, "def foo(): pass")]
    return DiffChunk(
        file_path=file_path,
        start_line=1,
        end_line=max(ln for ln, _ in added),
        added_lines=added,
        removed_lines=[],
        context_lines=[],
        raw_hunk="\n".join(f"+{c}" for _, c in added),
    )


def _finding(
    file: str = "app/main.py",
    line: int = 1,
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.85,
    message: str = "A potential issue was detected in the code path",
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category=FindingCategory.LOGIC,
        message=message,
        confidence=confidence,
    )


def _base_state(chunks: list[DiffChunk] | None = None) -> dict:
    """Minimal valid GraphState for the full graph."""
    return {
        "repo_full_name": "owner/test-repo",
        "pr_number": 99,
        "pr_title": "Test PR",
        "pr_author": "tester",
        "raw_diff": "--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n+def foo(): pass\n",
        "diff_files": chunks or [_chunk()],
        # LangGraph requires pre-initialised Annotated[list, add] fields
        "security_findings": [],
        "style_findings": [],
        "logic_findings": [],
        "test_findings": [],
        "errors": [],
        "node_timings": {},
    }


# ── _route_from_planner unit tests ────────────────────────────────────────────


def test_router_security_in_plan_dispatches_security_agent():
    state = _base_state()
    state["agent_plan"] = AgentPlan(agents_to_run=["security", "logic"])
    targets = _route_from_planner(state)
    assert "security_agent" in targets
    assert "logic_agent" in targets


def test_router_style_in_plan_dispatches_style_agent():
    state = _base_state()
    state["agent_plan"] = AgentPlan(agents_to_run=["style", "logic"])
    targets = _route_from_planner(state)
    assert "style_agent" in targets


def test_router_test_in_plan_dispatches_test_agent():
    state = _base_state()
    state["agent_plan"] = AgentPlan(agents_to_run=["test", "logic"])
    targets = _route_from_planner(state)
    assert "test_agent" in targets


def test_router_empty_plan_falls_back_to_logic():
    """An empty agents_to_run list must still dispatch logic_agent as safety fallback."""
    state = _base_state()
    state["agent_plan"] = AgentPlan(agents_to_run=[])
    targets = _route_from_planner(state)
    assert targets == ["logic_agent"]


def test_router_all_agents_dispatched():
    state = _base_state()
    state["agent_plan"] = AgentPlan(agents_to_run=["security", "style", "logic", "test"])
    targets = _route_from_planner(state)
    assert set(targets) == {"security_agent", "style_agent", "logic_agent", "test_agent"}


# ── aggregator_node unit tests ────────────────────────────────────────────────


def test_aggregator_builds_review_metadata():
    """aggregator_node must return a valid ReviewMetadata."""
    f = _finding()
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 5,
        "filtered_findings": [f],
        "all_findings": [f],
        "agent_plan": AgentPlan(agents_to_run=["logic"]),
        "node_timings": {"planner": 2.0, "logic_agent": 300.0, "critic": 50.0},
    }
    result = aggregator_node(state)
    assert "review_result" in result
    meta: ReviewMetadata = result["review_result"]
    assert meta.pr_number == 5
    assert meta.repo == "owner/repo"
    assert meta.total_findings_after_critic == 1
    assert meta.total_findings_raw == 1
    assert meta.agents_invoked == ["logic"]


def test_aggregator_total_latency_is_sum_of_timings():
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "filtered_findings": [],
        "all_findings": [],
        "agent_plan": AgentPlan(agents_to_run=["logic"]),
        "node_timings": {"planner": 10.0, "logic_agent": 200.0, "critic": 30.0},
    }
    result = aggregator_node(state)
    assert abs(result["review_result"].total_latency_ms - 240.0) < 1.0


def test_aggregator_estimates_non_zero_cost_for_invoked_agents():
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "filtered_findings": [],
        "all_findings": [],
        "agent_plan": AgentPlan(agents_to_run=["security", "logic"]),
        "node_timings": {},
    }
    result = aggregator_node(state)
    assert result["review_result"].estimated_cost_usd > 0


def test_aggregator_zero_cost_for_no_agents():
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "filtered_findings": [],
        "all_findings": [],
        "agent_plan": AgentPlan(agents_to_run=[]),
        "node_timings": {},
    }
    result = aggregator_node(state)
    assert result["review_result"].estimated_cost_usd == 0.0


def test_aggregator_returns_required_keys():
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "filtered_findings": [],
        "all_findings": [],
        "agent_plan": AgentPlan(agents_to_run=[]),
        "node_timings": {},
    }
    result = aggregator_node(state)
    assert "review_result" in result
    assert "node_timings" in result
    assert "aggregator" in result["node_timings"]
    assert "errors" in result


# ── Full graph integration tests (mocked Groq) ─────────────────────────────────


def _make_llm_response(findings_json: str) -> MagicMock:
    """Create a mock ChatGroq response returning the given JSON string."""
    mock_resp = MagicMock()
    mock_resp.content = findings_json
    return mock_resp


EMPTY_FINDINGS_JSON = "[]"

ONE_FINDING_JSON = json.dumps([
    {
        "file": "app/main.py",
        "line": 1,
        "severity": "high",
        "category": "logic",
        "message": "Possible unhandled exception in the main execution path",
        "confidence": 0.9,
    }
])


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_returns_review_metadata(mock_critic_llm, mock_agent_llm):
    """End-to-end: graph.invoke() with mocked LLMs returns ReviewMetadata."""
    mock_agent_llm.invoke.return_value = _make_llm_response(EMPTY_FINDINGS_JSON)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state()
    result = review_graph.invoke(state)

    assert result["review_result"] is not None
    assert isinstance(result["review_result"], ReviewMetadata)
    assert result["review_result"].repo == "owner/test-repo"
    assert result["review_result"].pr_number == 99


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_finding_flows_through_to_result(mock_critic_llm, mock_agent_llm):
    """A genuine finding from an agent should survive critic and appear in result."""
    mock_agent_llm.invoke.return_value = _make_llm_response(ONE_FINDING_JSON)
    # Critic sees 1 borderline finding (conf 0.9 >= CONFIDENCE_THRESHOLD → high conf, no LLM call)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state()
    result = review_graph.invoke(state)

    # total_findings_raw should include the finding from whichever agent ran
    meta: ReviewMetadata = result["review_result"]
    assert meta.total_findings_raw >= 1  # at least logic_agent ran
    assert meta.total_findings_after_critic >= 1


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_with_python_file_runs_style_agent(mock_critic_llm, mock_agent_llm):
    """A Python diff should trigger style, logic (and possibly test) agents."""
    mock_agent_llm.invoke.return_value = _make_llm_response(EMPTY_FINDINGS_JSON)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state(chunks=[_chunk("app/service.py")])
    result = review_graph.invoke(state)

    agents = result["review_result"].agents_invoked
    assert "logic" in agents
    assert "style" in agents


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_with_security_keyword_runs_security_agent(mock_critic_llm, mock_agent_llm):
    """A diff with a security keyword triggers the security agent."""
    mock_agent_llm.invoke.return_value = _make_llm_response(EMPTY_FINDINGS_JSON)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    dangerous_chunk = _chunk("app/auth.py", added_lines=[(1, "password = 'hardcoded_secret_123'")])
    state = _base_state(chunks=[dangerous_chunk])
    result = review_graph.invoke(state)

    agents = result["review_result"].agents_invoked
    assert "security" in agents


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_node_timings_populated(mock_critic_llm, mock_agent_llm):
    """node_timings should have an entry for every node that ran."""
    mock_agent_llm.invoke.return_value = _make_llm_response(EMPTY_FINDINGS_JSON)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state()
    result = review_graph.invoke(state)

    timings = result.get("node_timings", {})
    assert "planner" in timings
    assert "critic" in timings
    assert "aggregator" in timings


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_errors_list_is_empty_on_clean_run(mock_critic_llm, mock_agent_llm):
    """A clean run with mocked LLMs should produce no errors in the errors list."""
    mock_agent_llm.invoke.return_value = _make_llm_response(EMPTY_FINDINGS_JSON)
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state()
    result = review_graph.invoke(state)

    assert result.get("errors", []) == []


@patch("app.graph.nodes.base_agent._llm")
@patch("app.graph.nodes.critic._llm")
def test_full_graph_agent_error_does_not_crash_graph(mock_critic_llm, mock_agent_llm):
    """If an agent raises after retries, the graph should still complete (non-fatal)."""
    # Make logic_agent fail by returning unparseable JSON after all retries
    mock_agent_llm.invoke.return_value = _make_llm_response("this is not json at all !!!")
    mock_critic_llm.invoke.return_value = _make_llm_response("[]")

    state = _base_state()
    # Should not raise — graph must be resilient
    result = review_graph.invoke(state)
    assert result["review_result"] is not None
