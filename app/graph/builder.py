"""
Graph Builder
=============
Assembles the CodeSentinel LangGraph StateGraph.

Architecture:
  planner → [fan-out based on plan] → critic → aggregator

Fan-out nodes (specialist agents) run in PARALLEL via asyncio.gather.
LangGraph handles the fan-out/fan-in automatically when you add multiple
edges from the router — each target node executes concurrently.

Conditional routing (from planner):
  - Always: logic_agent
  - If security signals: security_agent
  - If Python/JS changed: style_agent
  - If source changed without tests: test_agent
  → All active agents → critic → aggregator → END
"""
from __future__ import annotations

import structlog
from langgraph.graph import END, START, StateGraph

from app.graph.nodes.aggregator import aggregator_node
from app.graph.nodes.critic import critic_node
from app.graph.nodes.logic_agent import logic_agent
from app.graph.nodes.planner import planner_node
from app.graph.nodes.security_agent import security_agent
from app.graph.nodes.style_agent import style_agent
from app.graph.nodes.test_agent import test_agent
from app.graph.state import GraphState

log = structlog.get_logger(__name__)


def _route_from_planner(state: GraphState) -> list[str]:
    """
    Conditional edge function. Returns list of node names to run in parallel.
    LangGraph will fan out to all returned nodes simultaneously.
    """
    plan = state["agent_plan"]
    agents = plan.agents_to_run

    targets: list[str] = []
    if "security" in agents:
        targets.append("security_agent")
    if "style" in agents:
        targets.append("style_agent")
    if "logic" in agents:
        targets.append("logic_agent")
    if "test" in agents:
        targets.append("test_agent")

    # Safety: always run at least logic
    if not targets:
        targets = ["logic_agent"]

    log.info("router.dispatch", targets=targets)
    return targets


def build_graph() -> StateGraph:
    """
    Build and compile the CodeSentinel review StateGraph.

    Returns a compiled graph ready for .invoke() or .ainvoke().
    """
    builder = StateGraph(GraphState)

    # ── Add nodes ─────────────────────────────────────────────────────────────
    builder.add_node("planner",         planner_node)
    builder.add_node("security_agent",  security_agent)
    builder.add_node("style_agent",     style_agent)
    builder.add_node("logic_agent",     logic_agent)
    builder.add_node("test_agent",      test_agent)
    builder.add_node("critic",          critic_node)
    builder.add_node("aggregator",      aggregator_node)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.add_edge(START, "planner")

    # ── Conditional fan-out from planner ──────────────────────────────────────
    # LangGraph runs all returned nodes in parallel (asyncio fan-out)
    builder.add_conditional_edges(
        "planner",
        _route_from_planner,
        {
            "security_agent": "security_agent",
            "style_agent":    "style_agent",
            "logic_agent":    "logic_agent",
            "test_agent":     "test_agent",
        },
    )

    # ── Fan-in: all agents → critic ──────────────────────────────────────────
    for agent_node in ("security_agent", "style_agent", "logic_agent", "test_agent"):
        builder.add_edge(agent_node, "critic")

    # ── Critic → Aggregator → END ─────────────────────────────────────────────
    builder.add_edge("critic", "aggregator")
    builder.add_edge("aggregator", END)

    return builder.compile()


# Module-level compiled graph — import this everywhere
review_graph = build_graph()
