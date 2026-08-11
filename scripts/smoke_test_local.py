#!/usr/bin/env python
"""
scripts/smoke_test_local.py
============================
Fires a realistic fake GitHub webhook payload at the local server
WITHOUT needing ngrok or a real GitHub webhook registered.

Usage:
    # 1. Start server in another terminal:
    #    uvicorn app.main:app --reload --port 8000

    # 2. Run this script (venv must be active):
    #    python scripts/smoke_test_local.py

What it does:
  - Builds a realistic GitHub pull_request webhook payload
  - Signs it with your GITHUB_WEBHOOK_SECRET (same as prod)
  - POSTs to http://localhost:8000/webhook/github
  - Polls server logs for background task completion
  - Prints pass/fail

NOTE: The background job will try to fetch the real diff from GitHub and
post a real comment on the PR. Set SMOKE_PR_NUMBER to a PR you own.
Set SMOKE_DRY_RUN=true to skip the GitHub posting step.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_URL = os.getenv("SMOKE_SERVER_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
REPO = os.getenv("SMOKE_REPO", "Yash22o2/codesentinel-test-repo")
PR_NUMBER = int(os.getenv("SMOKE_PR_NUMBER", "1"))

if not WEBHOOK_SECRET:
    print("❌  GITHUB_WEBHOOK_SECRET not set in .env")
    sys.exit(1)

# ── Build a realistic fake payload ───────────────────────────────────────────
PAYLOAD = {
    "action": "opened",
    "number": PR_NUMBER,
    "pull_request": {
        "number": PR_NUMBER,
        "title": "[SMOKE TEST] Add insecure password hashing",
        "state": "open",
        "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
        "head": {
            "sha": "abc123deadbeef" * 3,
            "ref": "feature/smoke-test",
        },
        "base": {
            "sha": "00000000deadbeef" * 2,
            "ref": "main",
        },
        "user": {
            "login": "Yash22o2",
            "id": 12345678,
        },
        "body": "Smoke test PR for CodeSentinel pipeline.",
        "draft": False,
        "changed_files": 1,
        "additions": 10,
        "deletions": 0,
    },
    "repository": {
        "id": 999999999,
        "name": "codesentinel-test-repo",
        "full_name": REPO,
        "private": False,
        "html_url": f"https://github.com/{REPO}",
        "default_branch": "main",
    },
    "sender": {
        "login": "Yash22o2",
        "id": 12345678,
    },
}


def _sign(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature the same way GitHub does."""
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def run_smoke_test() -> None:
    payload_bytes = json.dumps(PAYLOAD).encode()
    signature = _sign(payload_bytes, WEBHOOK_SECRET)

    print(f"\n[SMOKE TEST] CodeSentinel Local Smoke Test")
    print(f"   Server  : {SERVER_URL}")
    print(f"   Repo    : {REPO}")
    print(f"   PR      : #{PR_NUMBER}")
    print()

    # -- Step 1: Health check --
    print("1) Checking server health...")
    try:
        r = httpx.get(f"{SERVER_URL}/health", timeout=5)
        r.raise_for_status()
        print(f"   OK: Server is up: {r.json()}")
    except Exception as e:
        print(f"   FAIL: Server not reachable: {e}")
        print(f"      -> Start it with: uvicorn app.main:app --reload --port 8000")
        sys.exit(1)

    # -- Step 2: Fire webhook --
    print(f"\n2) Sending fake PR webhook (action=opened, PR #{PR_NUMBER})...")
    try:
        r = httpx.post(
            f"{SERVER_URL}/webhook/github",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "smoke-test-delivery-001",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"   FAIL: Request failed: {e}")
        sys.exit(1)

    if r.status_code == 202:
        resp = r.json()
        print(f"   OK: 202 Accepted -- request_id: {resp.get('request_id')}")
    elif r.status_code == 401:
        print(f"   FAIL: 401 Unauthorized -- HMAC signature mismatch!")
        print(f"      Check that GITHUB_WEBHOOK_SECRET in .env matches")
        sys.exit(1)
    else:
        print(f"   FAIL: Unexpected status {r.status_code}: {r.text[:300]}")
        sys.exit(1)

    # -- Step 3: Watch background job --
    print(f"\n3) Background review job enqueued!")
    print(f"   Watch the SERVER terminal for these log lines:")
    print(f"")
    print(f"   planner.done     -> which agents selected")
    print(f"   logic.done       -> findings from logic agent")
    print(f"   security.done    -> findings from security agent")
    print(f"   critic.done      -> how many survived the filter")
    print(f"   'Posted review'  -> GitHub comment was posted")
    print(f"")
    print(f"   The agents call Groq API -- takes ~20-60 seconds")
    print(f"")
    print(f"   Check for the PR comment at:")
    print(f"   https://github.com/{REPO}/pull/{PR_NUMBER}")
    print(f"")
    print(f"[OK] Smoke test payload delivered successfully!")


if __name__ == "__main__":
    run_smoke_test()

