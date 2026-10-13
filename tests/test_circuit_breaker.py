import time
import pytest
from services.circuit_breaker import CircuitBreaker, CircuitState, CircuitBreakerRegistry


def test_circuit_breaker_initial_state():
    cb = CircuitBreaker("test_service", failure_threshold=3, recovery_timeout=0.1)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True


def test_circuit_breaker_trips_to_open_after_threshold():
    cb = CircuitBreaker("test_service", failure_threshold=3, recovery_timeout=0.1)
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False


def test_circuit_breaker_recovery_to_half_open_and_closed():
    cb = CircuitBreaker("test_service", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False

    # Wait for recovery timeout
    time.sleep(0.06)
    assert cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Success in half open transitions to closed
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True


def test_circuit_breaker_probe_failure_re_opens():
    cb = CircuitBreaker("test_service", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.06)
    assert cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Probe fails -> immediately OPEN again
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False


def test_circuit_breaker_registry():
    registry = CircuitBreakerRegistry(default_threshold=2, default_recovery=10.0)
    assert registry.can_execute("gemini-3.1-flash-lite") is True

    registry.record_failure("gemini-3.1-flash-lite")
    registry.record_failure("gemini-3.1-flash-lite")

    assert registry.can_execute("gemini-3.1-flash-lite") is False
    assert registry.can_execute("gemini-3.6-flash") is True

    status = registry.get_status()
    assert status["gemini-3.1-flash-lite"] == "OPEN"
    assert status["gemini-3.6-flash"] == "CLOSED"
