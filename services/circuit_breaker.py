"""
Circuit Breaker module for external API resilience (Step 1.2).
Prevents cascading failures and timeout delays when external services (e.g. Gemini AI, MTProto)
experience temporary degradation or spikes in demand.
"""

import time
import logging
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "CLOSED"          # Normal operation: all requests allowed through
    OPEN = "OPEN"              # Tripped: fast-fail without calling remote service
    HALF_OPEN = "HALF_OPEN"    # Probing: test request allowed to verify recovery


class CircuitBreaker:
    """
    Manages failure tracking and state transitions for a single service/model.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 25.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count: int = 0
        self.state: CircuitState = CircuitState.CLOSED
        self.last_failure_time: float = 0.0
        self.total_trips: int = 0

    def can_execute(self) -> bool:
        """Returns True if request should proceed, False if circuit is OPEN (fast-fail)."""
        now = time.time()
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"⚡ [CircuitBreaker:{self.name}] Transitioning to HALF_OPEN (probing recovery).")
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            # Allow probe request
            return True

        return True

    def record_success(self):
        """Records successful execution and resets failure count to 0."""
        if self.state != CircuitState.CLOSED:
            logger.info(f"✅ [CircuitBreaker:{self.name}] Remote recovered! Transitioning to CLOSED.")
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        """Records failure and trips circuit to OPEN if threshold reached."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Probe failed, trip immediately back to OPEN
            self.state = CircuitState.OPEN
            self.total_trips += 1
            logger.warning(
                f"🚨 [CircuitBreaker:{self.name}] Probe failed in HALF_OPEN. Retripping to OPEN for {self.recovery_timeout}s."
            )
            return

        if self.failure_count >= self.failure_threshold and self.state == CircuitState.CLOSED:
            self.state = CircuitState.OPEN
            self.total_trips += 1
            logger.warning(
                f"🚨 [CircuitBreaker:{self.name}] Tripped to OPEN after {self.failure_count} consecutive failures. "
                f"Fast-failing for {self.recovery_timeout}s."
            )


class CircuitBreakerRegistry:
    """
    Thread-safe singleton registry managing circuit breakers by service/model name.
    """

    def __init__(self, default_threshold: int = 5, default_recovery: float = 25.0):
        self.default_threshold = default_threshold
        self.default_recovery = default_recovery
        self._breakers: Dict[str, CircuitBreaker] = {}

    def get_breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=self.default_threshold,
                recovery_timeout=self.default_recovery,
            )
        return self._breakers[name]

    def can_execute(self, name: str) -> bool:
        return self.get_breaker(name).can_execute()

    def record_success(self, name: str):
        self.get_breaker(name).record_success()

    def record_failure(self, name: str):
        self.get_breaker(name).record_failure()

    def get_status(self) -> Dict[str, str]:
        return {name: b.state.value for name, b in self._breakers.items()}


# Global singleton instance
circuit_registry = CircuitBreakerRegistry()
