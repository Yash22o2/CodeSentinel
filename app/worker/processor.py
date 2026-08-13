"""
Background Worker / Job Processor
===================================
Receives PRReviewJob from the webhook endpoint and runs the full
review pipeline:
  fetch diff → parse → LangGraph multi-agent review → post comment

Phase 3: Added total graph timeout (120s), job store tracking,
         and graph_deadline injection into graph state.
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
from app.reliability.job_store import job_store
from app.schemas import PRReviewJob, ReviewMetadata

logger = structlog.get_logger(__name__)
_settings = get_settings()


async def enqueue_review_job(job: PRReviewJob) -> None:
    """
    Entry point called by the webhook handler.
    Runs the full multi-agent review pipeline as a background task.

    Phase 3: Updates job store on start/complete/fail.
    """
    log = logger.bind(request_id=job.request_id, repo=job.repo_full_name, pr=job.pr_number)
    log.info("Starting review job")

    job_store.set_running(job.request_id)

    try:
        await _run_review(job)
        job_store.set_done(
            job.request_id,
            result={"repo": job.repo_full_name, "pr": job.pr_number},
        )
    except Exception as e:
        log.error("Review job failed", error=str(e), exc_info=True)
        job_store.set_failed(job.request_id, error=str(e))


async def _run_review(job: PRReviewJob) -> None:
    """
    Full Phase 2/3 LangGraph review pipeline:
      1. Fetch diff from GitHub
      2. Parse diff into structured DiffFile objects
      3. Invoke the LangGraph StateGraph (Planner → Agents → Critic → Aggregator)
         — wrapped in a total graph timeout (graph_timeout_s config)
      4. Post the final filtered findings as a GitHub PR review

    Phase 3 changes:
      - graph_deadline is injected into initial_state so each node can check it
      - The entire graph.invoke() is wrapped in asyncio.wait_for
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
    # Phase 3: compute a wall-clock deadline for the whole graph
    graph_deadline = time.monotonic() + _settings.graph_timeout_s

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
        # Phase 3: deadline shared with all nodes
        "graph_deadline":    graph_deadline,
    }

    # ── 4. Run LangGraph (async) with total timeout ───────────────────────────
    log.info(
        "Invoking LangGraph review graph",
        timeout_s=_settings.graph_timeout_s,
    )

    try:
        final_state = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: review_graph.invoke(initial_state),
            ),
            timeout=_settings.graph_timeout_s,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000
        log.error(
            "Graph timed out",
            timeout_s=_settings.graph_timeout_s,
            elapsed_ms=round(elapsed_ms, 1),
        )
        # Post a timeout notice to the PR so the developer knows
        await _post_timeout_comment(github, job)
        raise TimeoutError(
            f"Graph exceeded {_settings.graph_timeout_s}s timeout"
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

    # ── 7. Save to Database ───────────────────────────────────────────────────
    try:
        from sqlmodel import Session
        from app.db.session import engine
        from app.db.models import ReviewMetric

        security = sum(1 for f in filtered if f.category.value == "security")
        style = sum(1 for f in filtered if f.category.value == "style")
        logic = sum(1 for f in filtered if f.category.value == "logic")
        test = sum(1 for f in filtered if f.category.value == "test_coverage")

        with Session(engine) as session:
            metric = ReviewMetric(
                pr_id=f"{job.repo_full_name}#{job.pr_number}",
                total_latency_ms=int(metadata.total_latency_ms),
                total_tokens=metadata.total_tokens,
                estimated_cost_usd=metadata.estimated_cost_usd,
                security_findings_kept=security,
                style_findings_kept=style,
                logic_findings_kept=logic,
                test_findings_kept=test,
                created_at=datetime.utcnow()
            )
            session.add(metric)
            session.commit()
            log.info("Review metrics saved to dashboard DB")
    except Exception as e:
        log.error("Failed to save review metrics", error=str(e))

    log.info("Review complete", latency_ms=round(elapsed_ms, 1))


async def _post_timeout_comment(github, job: PRReviewJob) -> None:
    """Post a notice on the PR when the review times out."""
    try:
        from app.github.comment_poster import post_review
        from app.schemas import ReviewMetadata
        from datetime import datetime, timezone

        metadata = ReviewMetadata(
            request_id=job.request_id,
            repo=job.repo_full_name,
            pr_number=job.pr_number,
            total_findings_raw=0,
            total_findings_after_critic=0,
            agents_invoked=[],
            total_latency_ms=_settings.graph_timeout_s * 1000,
            estimated_cost_usd=0.0,
            completed_at=datetime.now(timezone.utc),
        )
        await post_review(
            github_client=github,
            repo_full_name=job.repo_full_name,
            pr_number=job.pr_number,
            pr_title=job.pr_title,
            commit_sha=job.head_sha,
            findings=[],
            metadata=metadata,
        )
    except Exception as e:
        logger.warning("Failed to post timeout comment", error=str(e))
