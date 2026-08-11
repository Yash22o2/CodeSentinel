"""
Tests: Circuit Breaker
======================
Tests for the CircuitBreaker class covering all state transitions.
"""
import time

import pytest

from app.reliability.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpenError,
    get_breaker,
    get_all_breaker_states,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cb(threshold: int = 3, recovery: float = 60.0) -> CircuitBreaker:
    """Create a fresh circuit breaker (not in the global registry)."""
    return CircuitBreaker(
        name="test_breaker",
        failure_threshold=threshold,
        recovery_timeout=recovery,
    )


def _fail_n_times(cb: CircuitBreaker, n: int) -> None:
    """Call cb.call() with a failing function n times."""
    for _ in range(n):
        with pytest.raises(Exception):
            cb.call(_always_fail)


def _always_fail():
    raise RuntimeError("Simulated failure")


def _always_succeed():
    return "OK"


# ── Tests: CLOSED state ────────────────────────────────────────────────────────

class TestClosedState:
    def test_starts_closed(self):
        cb = _make_cb()
        assert cb.state == BreakerState.CLOSED

    def test_successful_call_stays_closed(self):
        cb = _make_cb()
        result = cb.call(_always_succeed)
        assert result == "OK"
        assert cb.state == BreakerState.CLOSED

    def test_failure_increments_count(self):
        cb = _make_cb(threshold=3)
        with pytest.raises(RuntimeError):
            cb.call(_always_fail)
        assert cb.failure_count == 1
        assert cb.state == BreakerState.CLOSED  # Still closed, threshold not reached

    def test_failure_below_threshold_stays_closed(self):
        cb = _make_cb(threshold=3)
        _fail_n_times(cb, 2)
        assert cb.state == BreakerState.CLOSED

    def test_success_resets_failure_count(self):
        cb = _make_cb(threshold=3)
        _fail_n_times(cb, 2)
        cb.call(_always_succeed)
        assert cb.failure_count == 0


# ── Tests: CLOSED → OPEN transition ───────────────────────────────────────────

class TestOpenTransition:
    def test_trips_open_at_threshold(self):
        cb = _make_cb(threshold=3)
        _fail_n_times(cb, 3)
        assert cb.state == BreakerState.OPEN

    def test_open_rejects_calls_immediately(self):
        cb = _make_cb(threshold=3)
        _fail_n_times(cb, 3)
        with pytest.raises(CircuitBreakerOpenError):
            cb.call(_always_succeed)  # Would succeed, but breaker is open

    def test_open_error_has_name(self):
        cb = _make_cb(threshold=1)
        _fail_n_times(cb, 1)
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(_always_succeed)
        assert exc_info.value.name == "test_breaker"

    def test_open_error_has_retry_after(self):
        cb = _make_cb(threshold=1, recovery=30.0)
        _fail_n_times(cb, 1)
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(_always_succeed)
        assert exc_info.value.retry_after > 0
        assert exc_info.value.retry_after <= 30.0


# ── Tests: OPEN → HALF_OPEN transition ────────────────────────────────────────

class TestHalfOpenTransition:
    def test_transitions_to_half_open_after_recovery(self):
        cb = _make_cb(threshold=1, recovery=0.05)  # 50ms recovery
        _fail_n_times(cb, 1)
        assert cb.state == BreakerState.OPEN
        time.sleep(0.1)  # Wait for recovery
        assert cb.state == BreakerState.HALF_OPEN

    def test_successful_probe_closes_breaker(self):
        cb = _make_cb(threshold=1, recovery=0.05)
        _fail_n_times(cb, 1)
        time.sleep(0.1)
        assert cb.state == BreakerState.HALF_OPEN
        cb.call(_always_succeed)
        assert cb.state == BreakerState.CLOSED
        assert cb.failure_count == 0

    def test_failed_probe_reopens_breaker(self):
        cb = _make_cb(threshold=1, recovery=0.05)
        _fail_n_times(cb, 1)
        time.sleep(0.1)
        assert cb.state == BreakerState.HALF_OPEN
        with pytest.raises(RuntimeError):
            cb.call(_always_fail)
        assert cb.state == BreakerState.OPEN


# ── Tests: Manual reset ───────────────────────────────────────────────────────

class TestReset:
    def test_reset_from_open(self):
        cb = _make_cb(threshold=1)
        _fail_n_times(cb, 1)
        assert cb.state == BreakerState.OPEN
        cb.reset()
        assert cb.state == BreakerState.CLOSED
        assert cb.failure_count == 0

    def test_reset_allows_calls(self):
        cb = _make_cb(threshold=1)
        _fail_n_times(cb, 1)
        cb.reset()
        result = cb.call(_always_succeed)
        assert result == "OK"


# ── Tests: Global registry ────────────────────────────────────────────────────

class TestRegistry:
    def test_get_breaker_returns_same_instance(self):
        cb1 = get_breaker("shared_test_breaker", failure_threshold=5)
        cb2 = get_breaker("shared_test_breaker", failure_threshold=5)
        assert cb1 is cb2

    def test_get_all_breaker_states_returns_dict(self):
        get_breaker("state_test_breaker_1")
        get_breaker("state_test_breaker_2")
        states = get_all_breaker_states()
        assert isinstance(states, dict)
        assert "state_test_breaker_1" in states
        assert states["state_test_breaker_1"] in ("closed", "open", "half_open")
