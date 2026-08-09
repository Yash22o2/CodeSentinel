"""
Aggregator Node
===============
Final node before graph exit. Assembles the ReviewMetadata from filtered findings,
computes summary statistics, and prepares the payload for the comment poster.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from app.graph.state import GraphState
from app.schemas import Finding, ReviewMetadata, Severity

log = structlog.get_logger(__name__)


def aggregator_node(state: GraphState) -> dict:
    """Assemble the final ReviewMetadata from filtered findings."""
    t0 = time.monotonic()

    filtered: list[Finding] = state.get("filtered_findings", [])
    plan = state["agent_plan"]
    timings: dict[str, float] = state.get("node_timings", {})

    # ── Summary stats ─────────────────────────────────────────────────────────
    severity_counts: dict = {}
    for f in filtered:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    total_latency = sum(timings.values())

    # Rough token cost: llama-3.3-70b on Groq ~$0.59/1M tokens, ~2000 tokens/call
    estimated_cost = len(plan.agents_to_run) * 2000 * 0.59 / 1_000_000

    result = ReviewMetadata(
        request_id=str(uuid4()),
        repo=state["repo_full_name"],
        pr_number=state["pr_number"],
        total_findings_raw=len(state.get("all_findings", [])),
        total_findings_after_critic=len(filtered),
        agents_invoked=plan.agents_to_run,
        total_latency_ms=total_latency,
        estimated_cost_usd=estimated_cost,
        completed_at=datetime.now(timezone.utc),
    )

    elapsed = (time.monotonic() - t0) * 1000
    log.info(
        "aggregator.done",
        findings=len(filtered),
        critical=severity_counts.get(Severity.CRITICAL, 0),
        high=severity_counts.get(Severity.HIGH, 0),
        latency_ms=round(total_latency, 1),
        cost_usd=round(estimated_cost, 6),
    )

    return {
        "review_result": result,
        "node_timings": {"aggregator": elapsed},
        "errors": [],
    }
