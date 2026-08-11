"""
In-Memory Job Store
===================
Tracks the lifecycle of PR review jobs: pending → running → done | failed.

This is intentionally simple — a thread-safe dict. No Postgres, no Redis.
State is lost on server restart, which is acceptable for Phase 3.
Phase 6 will persist this to Postgres.

Usage:
    from app.reliability.job_store import job_store

    job_store.set_running("req-123")
    job_store.set_done("req-123", result={"findings": 5})
    job_store.set_failed("req-123", error="Timeout")
    info = job_store.get("req-123")
    # → {"status": "done", "result": {...}, "started_at": ..., "completed_at": ...}
"""
from __future__ import annotations

import threading
import time
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

JobStatus = Literal["pending", "running", "done", "failed"]


class InMemoryJobStore:
    """
    Thread-safe in-memory store for job lifecycle tracking.

    Keeps up to `max_size` entries; oldest entries are evicted when full
    (simple LRU-like behavior via insertion-order dict).
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    # ── Write operations ──────────────────────────────────────────────────────

    def set_pending(self, job_id: str, meta: dict | None = None) -> None:
        """Called when a job is enqueued but not yet picked up."""
        self._upsert(job_id, {
            "status": "pending",
            "enqueued_at": time.time(),
            "meta": meta or {},
        })

    def set_running(self, job_id: str) -> None:
        """Called when a worker picks up the job and starts executing."""
        self._upsert(job_id, {
            "status": "running",
            "started_at": time.time(),
        })
        log.info("job_store.running", job_id=job_id)

    def set_done(self, job_id: str, result: dict | None = None) -> None:
        """Called when the job finishes successfully."""
        self._upsert(job_id, {
            "status": "done",
            "completed_at": time.time(),
            "result": result or {},
        })
        log.info("job_store.done", job_id=job_id)

    def set_failed(self, job_id: str, error: str = "") -> None:
        """Called when the job fails (timeout, exception, etc.)."""
        self._upsert(job_id, {
            "status": "failed",
            "completed_at": time.time(),
            "error": error,
        })
        log.warning("job_store.failed", job_id=job_id, error=error)

    # ── Read operations ───────────────────────────────────────────────────────

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return the full job record, or None if not found."""
        with self._lock:
            return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def get_status(self, job_id: str) -> JobStatus | None:
        """Return just the status string, or None if not found."""
        with self._lock:
            entry = self._jobs.get(job_id)
            return entry["status"] if entry else None

    def all_jobs(self) -> dict[str, dict]:
        """Return a snapshot of all job records (for debug/health endpoint)."""
        with self._lock:
            return {jid: dict(rec) for jid, rec in self._jobs.items()}

    def count_by_status(self) -> dict[str, int]:
        """Return counts per status (pending/running/done/failed)."""
        with self._lock:
            counts: dict[str, int] = {}
            for rec in self._jobs.values():
                s = rec.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
            return counts

    # ── Internal ──────────────────────────────────────────────────────────────

    def _upsert(self, job_id: str, updates: dict) -> None:
        with self._lock:
            if job_id not in self._jobs:
                # Evict oldest entry if at capacity
                if len(self._jobs) >= self._max_size:
                    oldest = next(iter(self._jobs))
                    del self._jobs[oldest]
                self._jobs[job_id] = {}
            self._jobs[job_id].update(updates)


# ── Module-level singleton ────────────────────────────────────────────────────
# Import this everywhere: `from app.reliability.job_store import job_store`
job_store = InMemoryJobStore(max_size=1000)
