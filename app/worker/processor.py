"""
Background Worker / Job Processor
===================================
Receives PRReviewJob from the webhook endpoint and runs the full
review pipeline:
  fetch diff → parse → LangGraph multi-agent review → post comment

Phase 2: Real LangGraph execution with parallel specialist agents.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog

from app.config import get_settings
from app.github.client import get_github_client
from app.github.comment_poster import post_review
from app.github.diff_parser import parse_diff
from app.graph.builder import review_graph
from app.schemas import PRReviewJob, ReviewMetadata

logger = structlog.get_logger(__name__)


async def enqueue_review_job(job: PRReviewJob) -> None:
    """
    Entry point called by the webhook handler.
    Runs the full multi-agent review pipeline as a background task.
    """
    log = logger.bind(request_id=job.request_id, repo=job.repo_full_name, pr=job.pr_number)
    log.info("Starting review job")

    try:
        await _run_review(job)
    except Exception as e:
        log.error("Review job failed", error=str(e), exc_info=True)


async def _run_review(job: PRReviewJob) -> None:
    """
    Full Phase 2 LangGraph review pipeline:
      1. Fetch diff from GitHub
      2. Parse diff into structured DiffFile objects
      3. Invoke the LangGraph StateGraph (Planner → Agents → Critic → Aggregator)
      4. Post the final filtered findings as a GitHub PR review
    """
    log = logger.bind(request_id=job.request_id)
    start = time.monotonic()

    github = get_github_client()

    # ── 1. Fetch diff ─────────────────────────────────────────────────────────
    log.info("Fetching PR diff")
    diff_text = await github.get_pr_diff(job.repo_full_name, job.pr_number)
    job.diff = diff_text

    changed_files = github.get_changed_files(job.repo_full_name, job.pr_number)
    job.changed_files = changed_files
    log.info("Diff fetched", files=len(changed_files), bytes=len(diff_text))

    # ── 2. Parse diff ─────────────────────────────────────────────────────────
    parsed = parse_diff(diff_text)
    log.info(
        "Diff parsed",
        hunks=len(parsed.chunks),
        python=parsed.python_files,
        auth_patterns=parsed.has_auth_patterns,
    )

    # ── 3. Build initial graph state ──────────────────────────────────────────
    initial_state = {
        "repo_full_name": job.repo_full_name,
        "pr_number":      job.pr_number,
        "pr_title":       job.pr_title,
        "pr_author":      job.pr_author,
        "raw_diff":       diff_text,
        "diff_files":     parsed.chunks,
        # Pre-initialise list fields to empty (required for operator.add reducer)
        "security_findings": [],
        "style_findings":    [],
        "logic_findings":    [],
        "test_findings":     [],
        "all_findings":      [],
        "filtered_findings": [],
        "errors":            [],
        "node_timings":      {},
        "review_result":     None,
    }

    # ── 4. Run LangGraph (async) ───────────────────────────────────────────────
    log.info("Invoking LangGraph review graph")
    # LangGraph's ainvoke runs conditional fan-out nodes in parallel via asyncio
    final_state = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: review_graph.invoke(initial_state),
    )

    review_result = final_state.get("review_result")
    filtered = final_state.get("filtered_findings", [])
    timings = final_state.get("node_timings", {})
    errors = final_state.get("errors", [])
    plan = final_state.get("agent_plan")

    if errors:
        log.warning("Graph completed with errors", errors=errors)

    elapsed_ms = (time.monotonic() - start) * 1000
    log.info(
        "Graph complete",
        findings_raw=len(final_state.get("all_findings", [])),
        findings_filtered=len(filtered),
        agents=plan.agents_to_run if plan else [],
        elapsed_ms=round(elapsed_ms, 1),
    )

    # ── 5. Build metadata ─────────────────────────────────────────────────────
    metadata = ReviewMetadata(
        request_id=job.request_id,
        repo=job.repo_full_name,
        pr_number=job.pr_number,
        total_findings_raw=len(final_state.get("all_findings", [])),
        total_findings_after_critic=len(filtered),
        agents_invoked=plan.agents_to_run if plan else ["logic"],
        total_latency_ms=elapsed_ms,
        estimated_cost_usd=review_result.estimated_cost_usd if review_result else 0.0,
        completed_at=datetime.now(timezone.utc),
    )

    # ── 6. Post GitHub review ─────────────────────────────────────────────────
    log.info("Posting review", findings=len(filtered))
    await post_review(
        github_client=github,
        repo_full_name=job.repo_full_name,
        pr_number=job.pr_number,
        pr_title=job.pr_title,
        commit_sha=job.head_sha,
        findings=filtered,
        metadata=metadata,
    )

    log.info("Review complete", latency_ms=round(elapsed_ms, 1))
