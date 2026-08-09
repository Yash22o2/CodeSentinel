"""
GitHub Webhook Endpoint
========================
POST /webhook/github

Receives GitHub PR webhook events, validates the HMAC signature,
enqueues a review job to Redis, and immediately returns 202.

Critical design decision: return 202 within < 10 seconds.
GitHub will retry if it doesn't get a 2xx. The actual review
happens in the background worker.
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.github.webhook_validator import verify_webhook_signature
from app.schemas import GitHubWebhookPayload, PRReviewJob
from app.worker.processor import enqueue_review_job

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


async def _get_raw_body(request: Request) -> bytes:
    """Dependency: read raw bytes (must happen before Pydantic parsing)."""
    return await request.body()


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive GitHub PR webhook events",
    description=(
        "GitHub calls this endpoint when a PR is opened, updated, or re-opened. "
        "Validates HMAC-SHA256 signature, parses payload, enqueues review job, "
        "returns 202 immediately."
    ),
)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
) -> dict:
    request_id = x_github_delivery or str(uuid.uuid4())
    log = logger.bind(request_id=request_id, event=x_github_event)

    # ── 1. Read raw body ──────────────────────────────────────────────────────
    raw_body = await request.body()

    # ── 2. Validate HMAC signature ────────────────────────────────────────────
    if not verify_webhook_signature(raw_body, x_hub_signature_256, settings.github_webhook_secret):
        log.warning("Webhook signature validation failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # ── 3. Only handle pull_request events ───────────────────────────────────
    if x_github_event != "pull_request":
        log.info("Ignoring non-PR event", event_type=x_github_event)
        return {"status": "ignored", "reason": f"event type '{x_github_event}' not handled"}

    # ── 4. Parse and validate payload ────────────────────────────────────────
    try:
        import json
        payload_dict = json.loads(raw_body)
        payload = GitHubWebhookPayload.model_validate(payload_dict)
    except Exception as e:
        log.error("Failed to parse webhook payload", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid payload: {e}",
        ) from e

    log = log.bind(
        action=payload.action,
        repo=payload.repository.full_name,
        pr_number=payload.number,
    )

    # ── 5. Filter: only review actionable events ──────────────────────────────
    if not payload.should_review:
        log.info("Skipping review", reason=f"action={payload.action}, draft={payload.pull_request.draft}")
        return {
            "status": "skipped",
            "reason": f"action '{payload.action}' does not require review",
        }

    # ── 6. Build job and enqueue ──────────────────────────────────────────────
    job = PRReviewJob(
        request_id=request_id,
        repo_full_name=payload.repository.full_name,
        pr_number=payload.number,
        head_sha=payload.pull_request.head["sha"],
        base_sha=payload.pull_request.base["sha"],
        pr_title=payload.pull_request.title,
        pr_url=payload.pull_request.html_url,
        diff="",           # Worker fetches this — keeps webhook response fast
        changed_files=[],  # Worker fetches this too
    )

    background_tasks.add_task(enqueue_review_job, job)

    log.info("Review job enqueued", job_id=request_id)

    return {
        "status": "accepted",
        "request_id": request_id,
        "repo": payload.repository.full_name,
        "pr": payload.number,
    }
