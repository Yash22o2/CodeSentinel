"""
Graph State
===========
The single shared state object that flows through every LangGraph node.
All nodes read from and write to this TypedDict — LangGraph handles merging.
"""
from __future__ import annotations

from typing import Annotated, Any
import operator

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.github.diff_parser import DiffChunk
from app.schemas import AgentPlan, Finding, ReviewMetadata


def _merge_dicts(a: dict, b: dict) -> dict:
    """Reducer for node_timings: merges two timing dicts (last writer wins per key)."""
    return {**a, **b}


class GraphState(TypedDict):
    """
    Shared state for the CodeSentinel review graph.

    LangGraph uses TypedDict annotations to know how to merge parallel
    node outputs. Fields annotated with `operator.add` are list-merged
    (safe for fan-out); all others are last-write-wins.
    """

    # ── Input ─────────────────────────────────────────────────────────────────
    repo_full_name: str          # e.g. "Yash22o2/codesentinel-test-repo"
    pr_number: int
    pr_title: str
    pr_author: str
    raw_diff: str                # full unified diff text
    diff_files: list[DiffChunk]  # parsed structured diff chunks

    # ── Planner output ────────────────────────────────────────────────────────
    agent_plan: AgentPlan        # which agents to run + why

    # ── Agent findings (fan-out, merged with list concat) ────────────────────
    security_findings: Annotated[list[Finding], operator.add]
    style_findings:    Annotated[list[Finding], operator.add]
    logic_findings:    Annotated[list[Finding], operator.add]
    test_findings:     Annotated[list[Finding], operator.add]

    # ── Critic output ─────────────────────────────────────────────────────────
    all_findings: list[Finding]        # merged + deduplicated by critic
    filtered_findings: list[Finding]   # after false-positive suppression

    # ── Final output ──────────────────────────────────────────────────────────
    review_result: ReviewMetadata | None

    # ── Execution metadata ────────────────────────────────────────────────────
    errors: Annotated[list[str], operator.add]   # non-fatal errors from any node
    node_timings: Annotated[dict[str, float], _merge_dicts]  # node_name -> elapsed_ms
