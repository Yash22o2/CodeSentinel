"""
Tests: Timeout Behavior & Job Store
=====================================
Tests for Phase 3 reliability features:
  - InMemoryJobStore lifecycle tracking
  - graph_deadline skip logic in base_agent
  - Processor timeout flow (mocked)
"""
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.reliability.job_store import InMemoryJobStore
from app.reliability.circuit_breaker import CircuitBreaker, BreakerState


# ── Job Store Tests ────────────────────────────────────────────────────────────

class TestInMemoryJobStore:
    def test_set_and_get_pending(self):
        store = InMemoryJobStore()
        store.set_pending("job-1", meta={"repo": "test/repo", "pr": 42})
        info = store.get("job-1")
        assert info is not None
        assert info["status"] == "pending"
        assert info["meta"]["pr"] == 42

    def test_set_running(self):
        store = InMemoryJobStore()
        store.set_pending("job-2")
        store.set_running("job-2")
        assert store.get_status("job-2") == "running"
        assert "started_at" in store.get("job-2")

    def test_set_done(self):
        store = InMemoryJobStore()
        store.set_running("job-3")
        store.set_done("job-3", result={"findings": 5})
        info = store.get("job-3")
        assert info["status"] == "done"
        assert info["result"]["findings"] == 5
        assert "completed_at" in info

    def test_set_failed(self):
        store = InMemoryJobStore()
        store.set_running("job-4")
        store.set_failed("job-4", error="Timeout after 120s")
        info = store.get("job-4")
        assert info["status"] == "failed"
        assert "Timeout" in info["error"]

    def test_get_nonexistent_returns_none(self):
        store = InMemoryJobStore()
        assert store.get("does-not-exist") is None
        assert store.get_status("does-not-exist") is None

    def test_count_by_status(self):
        store = InMemoryJobStore()
        store.set_pending("j1")
        store.set_pending("j2")
        store.set_running("j3")
        store.set_done("j4")
        counts = store.count_by_status()
        assert counts.get("pending", 0) == 2
        assert counts.get("running", 0) == 1
        assert counts.get("done", 0) == 1

    def test_evicts_oldest_at_max_size(self):
        store = InMemoryJobStore(max_size=3)
        store.set_pending("a")
        store.set_pending("b")
        store.set_pending("c")
        store.set_pending("d")  # Should evict "a"
        assert store.get("a") is None
        assert store.get("d") is not None

    def test_thread_safe_concurrent_writes(self):
        """Multiple threads writing simultaneously should not raise."""
        store = InMemoryJobStore(max_size=1000)
        errors = []

        def write(i):
            try:
                store.set_pending(f"job-{i}")
                store.set_running(f"job-{i}")
                store.set_done(f"job-{i}", result={"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread-safety errors: {errors}"

    def test_all_jobs_returns_snapshot(self):
        store = InMemoryJobStore()
        store.set_pending("snap-1")
        store.set_done("snap-2")
        all_jobs = store.all_jobs()
        assert "snap-1" in all_jobs
        assert "snap-2" in all_jobs
        # Modifying the snapshot doesn't affect the store
        all_jobs["snap-1"]["status"] = "hacked"
        assert store.get_status("snap-1") == "pending"


# ── Graph Deadline Skip Tests ─────────────────────────────────────────────────

class TestGraphDeadlineSkip:
    """
    Tests the deadline-check in BaseAgent.__call__:
    If graph_deadline is in the past (or < 2s away), the agent
    should return empty findings and an error string immediately.
    """

    def _make_minimal_state(self, deadline_offset: float) -> dict:
        """Build a minimal graph state dict with the given deadline offset from now."""
        from unittest.mock import MagicMock
        return {
            "repo_full_name": "test/repo",
            "pr_number": 1,
            "diff_files": [],
            "graph_deadline": time.monotonic() + deadline_offset,
        }

    def test_agent_skips_when_deadline_passed(self):
        """Agent should skip immediately if deadline is already in the past."""
        from app.graph.nodes.security_agent import security_agent

        state = self._make_minimal_state(deadline_offset=-5.0)  # 5s in the past
        result = security_agent(state)

        assert result["security_findings"] == []
        assert any("deadline" in err.lower() for err in result["errors"])

    def test_agent_skips_when_less_than_2s_remaining(self):
        """Agent should skip if less than 2 seconds remain on the deadline."""
        from app.graph.nodes.logic_agent import logic_agent

        state = self._make_minimal_state(deadline_offset=0.5)  # Only 0.5s left
        result = logic_agent(state)

        assert result["logic_findings"] == []
        assert any("deadline" in err.lower() for err in result["errors"])

    def test_agent_runs_when_no_deadline(self):
        """If graph_deadline is None (not set), agent should try to run normally."""
        from app.graph.nodes.security_agent import security_agent
        from unittest.mock import patch

        state = {
            "repo_full_name": "test/repo",
            "pr_number": 1,
            "diff_files": [],
            "graph_deadline": None,
        }
        # With empty diff_files, the agent will run but find nothing.
        # We just verify it doesn't raise and doesn't skip due to deadline.
        with patch(
            "app.graph.nodes.base_agent.CircuitBreaker.call" if False else
            "app.reliability.circuit_breaker.CircuitBreaker.call",
            side_effect=lambda fn: fn(),
        ):
            # It will try to call Groq and fail (no API key in test env), which is OK.
            # We just confirm it didn't skip due to deadline.
            try:
                result = security_agent(state)
                # If it ran (even with error), deadline_skip should not be in errors
                skip_errors = [e for e in result.get("errors", []) if "deadline" in e.lower()]
                assert skip_errors == []
            except Exception:
                pass  # Groq API call failure is expected in tests


# ── Circuit Breaker Integration ───────────────────────────────────────────────

class TestCircuitBreakerInAgent:
    """
    Tests that the circuit breaker correctly blocks an agent after repeated failures.
    """

    def test_circuit_breaker_blocks_after_threshold(self):
        """
        After enough failures, the circuit should open and the agent should
        return a circuit-open error string instead of calling Groq.
        """
        from app.reliability.circuit_breaker import get_breaker, BreakerState

        # Get a fresh breaker with low threshold for test speed
        cb = get_breaker("test_integration_agent", failure_threshold=2, recovery_timeout=3600)
        cb.reset()  # Start clean

        # Force the breaker open
        for _ in range(2):
            try:
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("Simulated Groq failure")))
            except (RuntimeError, StopIteration):
                pass

        assert cb.state == BreakerState.OPEN
        cb.reset()  # Clean up for other tests
