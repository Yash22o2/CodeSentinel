"""
Tests for GitHub webhook endpoint.
Tests HMAC validation and payload routing logic.
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import get_settings

client = TestClient(app)
settings = get_settings()

WEBHOOK_SECRET = settings.github_webhook_secret


def _make_signature(payload: dict) -> str:
    body = json.dumps(payload).encode()
    digest = hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


SAMPLE_PR_PAYLOAD = {
    "action": "opened",
    "number": 1,
    "pull_request": {
        "number": 1,
        "title": "Add login feature",
        "state": "open",
        "html_url": "https://github.com/owner/repo/pull/1",
        "head": {"sha": "abc123", "ref": "feature/login"},
        "base": {"sha": "def456", "ref": "main"},
        "user": {"login": "developer", "id": 1},
        "body": "Adds login endpoint",
        "draft": False,
    },
    "repository": {
        "id": 1,
        "name": "repo",
        "full_name": "owner/repo",
        "private": False,
        "html_url": "https://github.com/owner/repo",
        "default_branch": "main",
    },
    "sender": {"login": "developer", "id": 1},
}


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_valid_signature_accepted():
    payload = json.dumps(SAMPLE_PR_PAYLOAD).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "test-delivery-id",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


def test_webhook_invalid_signature_rejected():
    payload = json.dumps(SAMPLE_PR_PAYLOAD).encode()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": "sha256=badhash",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_webhook_missing_signature_rejected():
    payload = json.dumps(SAMPLE_PR_PAYLOAD).encode()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_webhook_non_pr_event_ignored():
    payload = json.dumps({"action": "created"}).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


def test_webhook_draft_pr_skipped():
    payload_dict = {**SAMPLE_PR_PAYLOAD}
    payload_dict["pull_request"] = {**payload_dict["pull_request"], "draft": True}
    payload = json.dumps(payload_dict).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "skipped"
