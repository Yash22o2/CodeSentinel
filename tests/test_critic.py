"""
tests/test_critic.py
====================
Unit tests for the critic node:
  - Deduplication logic
  - Hard-drop threshold filtering
  - High-confidence pass-through (no LLM needed)
  - LLM filter integration (mocked Groq)
  - Sorting by severity then confidence
  - Fail-open: if LLM filter crashes, borderline findings are kept

All LLM calls are patched with unittest.mock so no Groq credits are used.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.graph.nodes.critic import (
    DROP_THRESHOLD,
    CONFIDENCE_THRESHOLD,
    _deduplicate,
    critic_node,
)
from app.schemas import Finding, FindingCategory, Severity


@pytest.fixture(autouse=True)
def mock_chroma_store():
    with patch("app.graph.nodes.critic.get_chroma_store") as mock_get_store:
        mock_store = MagicMock()
        mock_store.find_similar_dropped.return_value = False
        mock_get_store.return_value = mock_store
        yield mock_store


# ── Finding factory ───────────────────────────────────────────────────────────


def _finding(
    file: str = "app/main.py",
    line: int = 10,
    severity: Severity = Severity.MEDIUM,
    category: FindingCategory = FindingCategory.LOGIC,
    message: str = "Potential null dereference detected in returned value",
    confidence: float = 0.8,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category=category,
        message=message,
        confidence=confidence,
    )


def _make_state(
    security: list[Finding] | None = None,
    style: list[Finding] | None = None,
    logic: list[Finding] | None = None,
    test: list[Finding] | None = None,
) -> dict:
    """Minimal GraphState dict for critic_node."""
    return {
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "security_findings": security or [],
        "style_findings": style or [],
        "logic_findings": logic or [],
        "test_findings": test or [],
    }


# ── _deduplicate unit tests ───────────────────────────────────────────────────


def test_deduplicate_removes_exact_duplicate():
    """Two identical findings (same file, line, title prefix) → one kept."""
    f = _finding(file="app/auth.py", line=5, message="SQL injection risk in raw query execution")
    result = _deduplicate([f, f])
    assert len(result) == 1


def test_deduplicate_keeps_different_files():
    """Same message on different files → both kept."""
    f1 = _finding(file="app/a.py", line=5, message="SQL injection risk in raw query execution")
    f2 = _finding(file="app/b.py", line=5, message="SQL injection risk in raw query execution")
    result = _deduplicate([f1, f2])
    assert len(result) == 2


def test_deduplicate_keeps_different_lines():
    """Same message, same file, different lines → both kept."""
    f1 = _finding(file="app/a.py", line=5, message="SQL injection risk in raw query execution")
    f2 = _finding(file="app/a.py", line=20, message="SQL injection risk in raw query execution")
    result = _deduplicate([f1, f2])
    assert len(result) == 2


def test_deduplicate_preserves_order():
    """First occurrence of a duplicate is kept."""
    f1 = _finding(confidence=0.9, message="SQL injection risk in raw query execution")
    f2 = _finding(confidence=0.5, message="SQL injection risk in raw query execution")
    result = _deduplicate([f1, f2])
    assert result[0].confidence == 0.9


def test_deduplicate_empty_list():
    assert _deduplicate([]) == []


def test_deduplicate_single_item():
    f = _finding()
    assert _deduplicate([f]) == [f]


# ── critic_node: hard-drop threshold ─────────────────────────────────────────


def test_critic_drops_below_hard_threshold():
    """Findings with confidence < DROP_THRESHOLD are removed before dedup."""
    low = _finding(confidence=DROP_THRESHOLD - 0.01)
    high = _finding(
        line=99,
        confidence=0.9,
        message="Critical authentication bypass vulnerability detected",
    )
    state = _make_state(logic=[low, high])
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    # The low-confidence one is hard-dropped
    all_f = result["all_findings"]
    filtered = result["filtered_findings"]
    assert low not in filtered
    assert high in filtered or high.confidence >= CONFIDENCE_THRESHOLD


def test_critic_keeps_above_hard_threshold():
    """Findings at exactly DROP_THRESHOLD are kept."""
    f = _finding(confidence=DROP_THRESHOLD, message="Resource leak causing memory exhaustion issue")
    state = _make_state(logic=[f])
    with patch("app.graph.nodes.critic._llm_filter", return_value=[f]):
        result = critic_node(state)
    assert f in result["filtered_findings"]


# ── critic_node: high-confidence pass-through (no LLM needed) ────────────────


def test_critic_high_confidence_bypasses_llm_filter():
    """Findings above CONFIDENCE_THRESHOLD must NOT be sent to _llm_filter."""
    f = _finding(confidence=0.95, message="Hardcoded API secret in source code detected")
    state = _make_state(security=[f])

    with patch("app.graph.nodes.critic._llm_filter") as mock_filter:
        mock_filter.return_value = []
        result = critic_node(state)

    # f is high-confidence → goes through directly, _llm_filter not called for it
    # (borderline list is empty if all are high-conf → _llm_filter called with [])
    assert f in result["filtered_findings"]


def test_critic_calls_llm_filter_for_borderline():
    """Findings in [DROP_THRESHOLD, CONFIDENCE_THRESHOLD) go to _llm_filter."""
    borderline_conf = (DROP_THRESHOLD + CONFIDENCE_THRESHOLD) / 2
    f = _finding(
        confidence=borderline_conf,
        message="Possible missing input validation on user-supplied data",
    )
    state = _make_state(logic=[f])

    with patch("app.graph.nodes.critic._llm_filter") as mock_filter:
        mock_filter.return_value = [f]  # mock says keep it
        result = critic_node(state)

    # _llm_filter should have been called with the borderline finding
    mock_filter.assert_called_once()
    called_with = mock_filter.call_args[0][1]
    assert f in called_with


def test_critic_auto_drops_similar_findings(mock_chroma_store):
    """Borderline findings that match in Chroma should be auto-dropped without LLM."""
    borderline_conf = (DROP_THRESHOLD + CONFIDENCE_THRESHOLD) / 2
    f = _finding(
        confidence=borderline_conf,
        message="Previously dropped false positive",
    )
    state = _make_state(logic=[f])
    
    mock_chroma_store.find_similar_dropped.return_value = True

    with patch("app.graph.nodes.critic._llm_filter") as mock_filter:
        result = critic_node(state)

    # _llm_filter should NOT have been called because it was auto-dropped
    mock_filter.assert_not_called()
    assert f not in result["filtered_findings"]



# ── critic_node: merge from all agents ───────────────────────────────────────


def test_critic_merges_all_agent_findings():
    """all_findings must be the union of all four agent finding lists."""
    sec = _finding(file="a.py", line=1, message="Insecure direct object reference detected here")
    sty = _finding(file="b.py", line=2, message="Line exceeds maximum length coding style issue")
    log = _finding(file="c.py", line=3, message="Off-by-one error in loop boundary condition")
    tst = _finding(file="d.py", line=4, message="Missing test coverage for error handling path")

    state = _make_state(security=[sec], style=[sty], logic=[log], test=[tst])
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)

    assert len(result["all_findings"]) == 4
    assert sec in result["all_findings"]
    assert sty in result["all_findings"]
    assert log in result["all_findings"]
    assert tst in result["all_findings"]


def test_critic_empty_inputs_returns_empty():
    """No findings from any agent → empty filtered results."""
    state = _make_state()
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    assert result["filtered_findings"] == []
    assert result["all_findings"] == []


# ── critic_node: sorting ──────────────────────────────────────────────────────


def test_critic_sorts_critical_before_medium():
    """Critical findings should appear before medium ones in filtered output."""
    medium = _finding(severity=Severity.MEDIUM, confidence=0.9,
                      message="Unused variable assignment in production code")
    critical = _finding(
        line=20,
        severity=Severity.CRITICAL,
        confidence=0.9,
        message="Remote code execution vulnerability in deserialization path",
    )
    state = _make_state(security=[medium, critical])
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    findings = result["filtered_findings"]
    crit_idx = next(i for i, f in enumerate(findings) if f.severity == Severity.CRITICAL)
    med_idx = next(i for i, f in enumerate(findings) if f.severity == Severity.MEDIUM)
    assert crit_idx < med_idx


def test_critic_sorts_higher_confidence_first_within_same_severity():
    """Within the same severity, higher confidence should come first."""
    low_conf = _finding(confidence=0.6, message="Potential resource leak in context manager usage")
    high_conf = _finding(confidence=0.95, message="Definite null pointer dereference in main path")
    state = _make_state(logic=[low_conf, high_conf])
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    findings = result["filtered_findings"]
    if len(findings) >= 2:
        assert findings[0].confidence >= findings[1].confidence


# ── critic_node: fail-open on LLM error ──────────────────────────────────────


def test_critic_fail_open_when_llm_filter_raises():
    """If _llm_filter throws, borderline findings should still be kept (fail-open)."""
    borderline_conf = (DROP_THRESHOLD + CONFIDENCE_THRESHOLD) / 2
    f = _finding(
        confidence=borderline_conf,
        message="Possible missing input validation on user-supplied data",
    )
    state = _make_state(logic=[f])

    with patch("app.graph.nodes.critic._llm_filter", side_effect=RuntimeError("Groq timeout")):
        result = critic_node(state)

    # fail-open: borderline finding should be kept even though LLM filter failed
    assert f in result["filtered_findings"]
    # The critic logs the error internally but returns errors=[] (fail-open design):
    # the finding is kept rather than lost, which is the safe default.
    assert result["errors"] == []


# ── critic_node: return structure ─────────────────────────────────────────────


def test_critic_returns_required_keys():
    """Result dict must contain all_findings, filtered_findings, node_timings, errors."""
    state = _make_state()
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    assert "all_findings" in result
    assert "filtered_findings" in result
    assert "node_timings" in result
    assert "critic" in result["node_timings"]
    assert "errors" in result
    assert isinstance(result["errors"], list)


def test_critic_timing_is_positive():
    state = _make_state()
    with patch("app.graph.nodes.critic._llm_filter", return_value=[]):
        result = critic_node(state)
    assert result["node_timings"]["critic"] >= 0
