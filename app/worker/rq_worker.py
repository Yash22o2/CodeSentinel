"""
RQ Worker Entry Point
=====================
This module provides the plain sync function that RQ calls when it picks
up a job from the Redis queue.

Why a separate module from processor.py?
  - RQ requires a plain (non-async) function as the job entry point
  - This thin wrapper bridges the sync RQ world → async processor world
  - processor.py keeps its async interface for direct BackgroundTask usage

How to run the worker:
    # Activate venv, then:
    rq worker codesentinel --url redis://localhost:6379/0

Or via Docker Compose (add a worker service):
    command: rq worker codesentinel --url redis://redis:6379/0
"""
from __future__ import annotations

import asyncio
import logging

import structlog

from app.reliability.job_store import job_store
from app.schemas import PRReviewJob

log = structlog.get_logger(__name__)


def process_pr_review(job_dict: dict) -> None:
    """
    RQ job entry point — called by the rq worker process.

    Args:
        job_dict: The PRReviewJob serialised as a plain dict
                  (RQ passes simple types; Pydantic objects are re-hydrated here).

    This function:
    1. Re-hydrates the PRReviewJob from the dict
    2. Runs the async review pipeline in a fresh event loop
    3. Updates the job store (running → done | failed)
    """
    job = PRReviewJob.model_validate(job_dict)
    log_ctx = log.bind(request_id=job.request_id, repo=job.repo_full_name, pr=job.pr_number)
    log_ctx.info("rq_worker.picked_up")

    job_store.set_running(job.request_id)

    # RQ workers run in a plain sync context; we create a fresh event loop.
    try:
        asyncio.run(_run(job))
        job_store.set_done(
            job.request_id,
            result={"repo": job.repo_full_name, "pr": job.pr_number},
        )
        log_ctx.info("rq_worker.done")
    except Exception as exc:
        job_store.set_failed(job.request_id, error=str(exc))
        log_ctx.error("rq_worker.failed", error=str(exc))
        raise  # Re-raise so RQ marks the job as failed in its own registry


async def _run(job: PRReviewJob) -> None:
    """Thin async bridge — delegates to the main processor pipeline."""
    from app.worker.processor import _run_review
    await _run_review(job)
