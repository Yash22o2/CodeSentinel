"""
Background Worker / Job Processor
===================================
Receives PRReviewJob from the webhook endpoint and runs the full
review pipeline (fetch diff → run graph → post comment).

For Phase 1, this runs as a FastAPI BackgroundTask (simple, no Redis needed).
Phase 2+ will move this to a proper Redis queue worker (RQ/Celery).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import structlog

from app.config import get_settings
from app.github.client import get_github_client
from app.github.comment_poster import post_review
from app.github.diff_parser import parse_diff
from app.schemas import PRReviewJob, ReviewMetadata

logger = structlog.get_logger(__name__)


async def enqueue_review_job(job: PRReviewJob) -> None:
    """
    Entry point called by the webhook handler.
    Phase 1: runs the review inline as a background task.
    Phase 2: will push to Redis queue and return immediately.
    """
    log = logger.bind(request_id=job.request_id, repo=job.repo_full_name, pr=job.pr_number)
    log.info("Starting review job")

    try:
        await _run_review(job)
    except Exception as e:
        log.error("Review job failed", error=str(e), exc_info=True)


async def _run_review(job: PRReviewJob) -> None:
    """
    Phase 1 single-agent review pipeline.
    Later replaced by the LangGraph runner.
    """
    settings = get_settings()
    log = logger.bind(request_id=job.request_id)
    start_ms = time.monotonic() * 1000

    github = get_github_client()

    # ── 1. Fetch diff and changed files ──────────────────────────────────────
    log.info("Fetching PR diff")
    diff_text = await github.get_pr_diff(job.repo_full_name, job.pr_number)
    job.diff = diff_text

    changed_files = github.get_changed_files(job.repo_full_name, job.pr_number)
    job.changed_files = changed_files
    log.info("Diff fetched", files_changed=len(changed_files), diff_bytes=len(diff_text))

    # ── 2. Parse diff ─────────────────────────────────────────────────────────
    parsed_diff = parse_diff(diff_text)
    log.info(
        "Diff parsed",
        chunks=len(parsed_diff.chunks),
        python_files=parsed_diff.python_files,
        has_auth_patterns=parsed_diff.has_auth_patterns,
    )

    # ── 3. Run LangGraph (Phase 2) — placeholder for now ─────────────────────
    # In Phase 1 we just demonstrate the end-to-end loop with a placeholder review.
    # The real graph runner will replace this block entirely.
    findings = []  # Will come from graph.runner.run(job, parsed_diff) in Phase 2

    # ── 4. Build metadata ─────────────────────────────────────────────────────
    elapsed_ms = time.monotonic() * 1000 - start_ms
    metadata = ReviewMetadata(
        request_id=job.request_id,
        repo=job.repo_full_name,
        pr_number=job.pr_number,
        total_findings_raw=len(findings),
        total_findings_after_critic=len(findings),
        agents_invoked=["placeholder"],  # Will be real agents in Phase 2
        total_latency_ms=elapsed_ms,
        estimated_cost_usd=0.0,
        completed_at=datetime.now(timezone.utc),
    )

    # ── 5. Post review ────────────────────────────────────────────────────────
    log.info("Posting review", findings=len(findings), latency_ms=elapsed_ms)
    await post_review(
        github_client=github,
        repo_full_name=job.repo_full_name,
        pr_number=job.pr_number,
        pr_title=job.pr_title,
        commit_sha=job.head_sha,
        findings=findings,
        metadata=metadata,
    )

    log.info("Review complete", latency_ms=elapsed_ms)
