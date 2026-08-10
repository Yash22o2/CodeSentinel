"""
tests/test_planner.py
=====================
Unit tests for the planner_node routing heuristics.
No LLM calls are made — planner is purely deterministic.
"""
from __future__ import annotations

import pytest

from app.github.diff_parser import DiffChunk
from app.graph.nodes.planner import planner_node
from app.schemas import AgentPlan


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_chunk(
    file_path: str,
    added_lines: list[tuple[int, str]] | None = None,
) -> DiffChunk:
    """Minimal DiffChunk factory for tests."""
    added = added_lines or []
    return DiffChunk(
        file_path=file_path,
        start_line=1,
        end_line=max((ln for ln, _ in added), default=1),
        added_lines=added,
        removed_lines=[],
        context_lines=[],
        raw_hunk="",
    )


def _make_state(chunks: list[DiffChunk]) -> dict:
    """Build the minimum GraphState dict the planner needs."""
    return {
        "repo_full_name": "owner/repo",
        "pr_number": 42,
        "pr_title": "Test PR",
        "pr_author": "tester",
        "raw_diff": "",
        "diff_files": chunks,
    }


# ── Basic routing ─────────────────────────────────────────────────────────────


def test_planner_always_runs_logic():
    """Logic agent must always be in the plan, regardless of diff contents."""
    state = _make_state([_make_chunk("README.md")])
    result = planner_node(state)
    plan: AgentPlan = result["agent_plan"]
    assert "logic" in plan.agents_to_run


def test_planner_returns_agent_plan():
    """Return dict must contain a valid AgentPlan object."""
    state = _make_state([_make_chunk("app/main.py")])
    result = planner_node(state)
    assert isinstance(result["agent_plan"], AgentPlan)
    assert isinstance(result["agent_plan"].agents_to_run, list)
    assert result["agent_plan"].rationale != ""


def test_planner_records_timing():
    """node_timings must include a 'planner' entry with a positive float."""
    state = _make_state([_make_chunk("app/main.py")])
    result = planner_node(state)
    assert "planner" in result["node_timings"]
    assert result["node_timings"]["planner"] >= 0


def test_planner_errors_is_empty_list():
    """errors key must always be present and be an empty list on success."""
    state = _make_state([_make_chunk("app/main.py")])
    result = planner_node(state)
    assert result["errors"] == []


# ── Style agent routing ───────────────────────────────────────────────────────


def test_planner_routes_style_for_python():
    """A .py file should trigger the style agent."""
    state = _make_state([_make_chunk("app/service.py")])
    result = planner_node(state)
    assert "style" in result["agent_plan"].agents_to_run


def test_planner_routes_style_for_js():
    """A .js file should trigger the style agent."""
    state = _make_state([_make_chunk("frontend/index.js")])
    result = planner_node(state)
    assert "style" in result["agent_plan"].agents_to_run


def test_planner_routes_style_for_ts():
    """A .ts / .tsx file should trigger the style agent."""
    state = _make_state([_make_chunk("src/App.tsx")])
    result = planner_node(state)
    assert "style" in result["agent_plan"].agents_to_run


def test_planner_no_style_for_markdown():
    """A markdown-only diff should NOT trigger the style agent."""
    state = _make_state([_make_chunk("README.md")])
    result = planner_node(state)
    assert "style" not in result["agent_plan"].agents_to_run


# ── Security agent routing ────────────────────────────────────────────────────


def test_planner_routes_security_for_config_file():
    """A .env file change should trigger security."""
    state = _make_state([_make_chunk("config/.env")])
    result = planner_node(state)
    assert "security" in result["agent_plan"].agents_to_run


def test_planner_routes_security_for_yaml():
    """A .yml file change should trigger security."""
    state = _make_state([_make_chunk("docker-compose.yml")])
    result = planner_node(state)
    assert "security" in result["agent_plan"].agents_to_run


@pytest.mark.parametrize("keyword,line", [
    ("password", "password = 'hunter2'"),
    ("secret", "SECRET_KEY = 'abc123'"),
    ("eval(", "eval(user_input)"),
    ("subprocess", "subprocess.run(cmd, shell=True)"),
    ("pickle", "data = pickle.loads(raw)"),
    ("yaml.load", "config = yaml.load(f)"),
    ("hashlib.md5", "h = hashlib.md5(pw)"),
    ("sql", "cursor.execute(sql)"),
])
def test_planner_routes_security_for_keyword_in_added_line(keyword, line):
    """Security keywords in added lines should trigger the security agent."""
    chunk = _make_chunk("app/service.py", added_lines=[(10, line)])
    state = _make_state([chunk])
    result = planner_node(state)
    assert "security" in result["agent_plan"].agents_to_run, (
        f"Expected security agent for keyword '{keyword}' in line: {line!r}"
    )


def test_planner_no_security_for_benign_python():
    """Clean Python code without security signals should NOT trigger security."""
    chunk = _make_chunk("app/utils.py", added_lines=[(1, "def add(a, b): return a + b")])
    state = _make_state([chunk])
    result = planner_node(state)
    assert "security" not in result["agent_plan"].agents_to_run


# ── Test agent routing ────────────────────────────────────────────────────────


def test_planner_routes_test_agent_when_source_changed_without_tests():
    """Python source change with no test file → test agent should run."""
    chunk = _make_chunk("app/service.py", added_lines=[(1, "def foo(): pass")])
    state = _make_state([chunk])
    result = planner_node(state)
    assert "test" in result["agent_plan"].agents_to_run


def test_planner_no_test_agent_when_test_file_present():
    """If a test file is also changed, test agent should NOT be flagged."""
    source_chunk = _make_chunk("app/service.py", added_lines=[(1, "def foo(): pass")])
    test_chunk = _make_chunk("tests/test_service.py", added_lines=[(1, "def test_foo(): pass")])
    state = _make_state([source_chunk, test_chunk])
    result = planner_node(state)
    assert "test" not in result["agent_plan"].agents_to_run


def test_planner_no_test_agent_for_markdown_only():
    """Markdown-only diff should never trigger the test agent."""
    state = _make_state([_make_chunk("README.md")])
    result = planner_node(state)
    assert "test" not in result["agent_plan"].agents_to_run


# ── Safety fallback ───────────────────────────────────────────────────────────


def test_planner_fallback_on_empty_diff():
    """Even an empty diff must return at least logic in the agent plan."""
    state = _make_state([])
    result = planner_node(state)
    assert "logic" in result["agent_plan"].agents_to_run


def test_planner_diff_summary_includes_file_count():
    """diff_summary should mention how many files changed."""
    chunks = [_make_chunk("a.py"), _make_chunk("b.py")]
    state = _make_state(chunks)
    result = planner_node(state)
    assert "2" in result["agent_plan"].diff_summary
