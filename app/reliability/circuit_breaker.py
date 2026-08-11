"""
Circuit Breaker
===============
A per-agent circuit breaker that prevents hammering a failing downstream API.

States:
  CLOSED    → normal operation, requests go through
  OPEN      → too many failures, requests fail immediately (no API call)
  HALF_OPEN → cooldown elapsed, one probe request allowed through

Usage:
    cb = CircuitBreaker(name="security_agent", failure_threshold=5, recovery_timeout=60)

    @cb.call
    def my_function():
        return call_groq_api()

Or as a context manager:
    with cb:
        call_groq_api()

The breaker is process-level (module-level registry).
A shared registry allows webhook + worker to use the same breaker instances.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"       # Normal: requests pass through
    OPEN = "open"           # Failing: requests blocked immediately
    HALF_OPEN = "half_open" # Probing: one request allowed through


class CircuitBreakerOpenError(Exception):
    """Raised when a call is attempted on an OPEN circuit breaker."""

    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit '{name}' is OPEN. Retry after {retry_after:.1f}s."
        )


class CircuitBreaker:
    """
    Thread-safe circuit breaker with automatic half-open probing.

    Args:
        name:              Identifier for logging (e.g. "security_agent")
        failure_threshold: Consecutive failures before tripping OPEN (default 5)
        recovery_timeout:  Seconds to stay OPEN before probing (default 60)
        expected_exception: Exception type(s) that count as failures.
                            Defaults to Exception (all exceptions).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type[Exception] | tuple[type[Exception], ...] = Exception,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._get_state()

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def call(self, func: Callable[[], T]) -> T:
        """
        Execute `func` through the circuit breaker.
        Raises CircuitBreakerOpenError if the breaker is OPEN.
        """
        with self._lock:
            current_state = self._get_state()

            if current_state == BreakerState.OPEN:
                retry_after = self._time_until_recovery()
                log.warning(
                    "circuit_breaker.blocked",
                    breaker=self.name,
                    state=current_state,
                    retry_after_s=round(retry_after, 1),
                )
                raise CircuitBreakerOpenError(self.name, retry_after)

            # CLOSED or HALF_OPEN: allow the call through
            if current_state == BreakerState.HALF_OPEN:
                log.info("circuit_breaker.probe", breaker=self.name)

        # Execute outside the lock so we don't hold it during I/O
        try:
            result = func()
            self._on_success()
            return result
        except self.expected_exception as exc:
            self._on_failure()
            raise

    def reset(self) -> None:
        """Manually reset to CLOSED (useful for tests)."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
        log.info("circuit_breaker.reset", breaker=self.name)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_state(self) -> BreakerState:
        """
        State transition logic (called inside lock):
        OPEN → HALF_OPEN if recovery_timeout has elapsed.
        """
        if self._state == BreakerState.OPEN:
            if self._last_failure_time is not None:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = BreakerState.HALF_OPEN
                    log.info(
                        "circuit_breaker.half_open",
                        breaker=self.name,
                        elapsed_s=round(elapsed, 1),
                    )
        return self._state

    def _time_until_recovery(self) -> float:
        if self._last_failure_time is None:
            return 0.0
        elapsed = time.monotonic() - self._last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)

    def _on_success(self) -> None:
        with self._lock:
            prev_state = self._state
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            if prev_state == BreakerState.HALF_OPEN:
                log.info("circuit_breaker.recovered", breaker=self.name)

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == BreakerState.HALF_OPEN:
                # Probe failed → stay OPEN
                self._state = BreakerState.OPEN
                log.warning(
                    "circuit_breaker.probe_failed",
                    breaker=self.name,
                    failures=self._failure_count,
                )
            elif self._failure_count >= self.failure_threshold:
                self._state = BreakerState.OPEN
                log.warning(
                    "circuit_breaker.opened",
                    breaker=self.name,
                    failures=self._failure_count,
                    threshold=self.failure_threshold,
                )


# ── Module-level registry: one breaker per agent ──────────────────────────────
# These are process-level singletons shared across all requests.

_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
) -> CircuitBreaker:
    """
    Get (or create) a named circuit breaker from the global registry.
    Always use this instead of constructing CircuitBreaker() directly
    so the same instance is reused across calls.
    """
    with _registry_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _breakers[name]


def get_all_breaker_states() -> dict[str, str]:
    """Return a snapshot of all breaker states for health/debug endpoints."""
    with _registry_lock:
        return {name: cb.state.value for name, cb in _breakers.items()}
