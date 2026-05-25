"""Layer-2 resilience: 3-state circuit breaker (closed -> open -> half_open -> closed).

Consolidates Munshi's internal pattern and Nowlez's `backend.scraper_resilience.CircuitBreaker`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import threading
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from ecourts_client.errors import CircuitOpen


T = TypeVar("T")
logger = logging.getLogger(__name__)


class CircuitBreaker:
    """3-state circuit breaker.

    Args:
        name: identifier for metrics/logs.
        failure_threshold: consecutive failures before opening.
        recovery_timeout: seconds before transitioning open -> half_open.
        max_recovery_timeout: ceiling on exponential back-off in half_open re-open.
        on_open: optional async callback (failure_count, recovery_timeout) invoked once per open transition.
    """
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        max_recovery_timeout: float = 1800.0,
        on_open: Callable[[int, float], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._base_recovery_timeout = recovery_timeout
        self._current_recovery_timeout = recovery_timeout
        self._max_recovery_timeout = max_recovery_timeout
        self._on_open = on_open
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state: str = "closed"
        self._half_open_allowed = False
        self._half_open_time = 0.0
        self._probe_failures = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._state == "open" and (now - self._last_failure_time) >= self._current_recovery_timeout:
                self._state = "half_open"
                self._half_open_allowed = True
                self._half_open_time = now
            elif self._state == "half_open" and not self._half_open_allowed:
                if (now - self._half_open_time) >= self._base_recovery_timeout:
                    self._half_open_allowed = True
                    self._half_open_time = now
            return self._state

    def allow_request(self) -> bool:
        # Single critical section: do the state transition AND the
        # half_open_allowed write atomically (nested re-acquire would
        # require RLock).
        with self._lock:
            now = time.monotonic()
            if self._state == "open" and (now - self._last_failure_time) >= self._current_recovery_timeout:
                self._state = "half_open"
                self._half_open_allowed = True
                self._half_open_time = now
            elif self._state == "half_open" and not self._half_open_allowed:
                if (now - self._half_open_time) >= self._base_recovery_timeout:
                    self._half_open_allowed = True
                    self._half_open_time = now
            s = self._state
            if s == "closed":
                return True
            if s == "half_open" and self._half_open_allowed:
                self._half_open_allowed = False
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._probe_failures = 0
                self._current_recovery_timeout = self._base_recovery_timeout
            self._failure_count = 0
            self._state = "closed"
            self._half_open_allowed = False

    def record_failure(self) -> None:
        fire = False
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == "half_open":
                self._probe_failures += 1
                self._current_recovery_timeout = min(
                    self._base_recovery_timeout * (2 ** self._probe_failures),
                    self._max_recovery_timeout,
                )
                self._state = "open"
                fire = True
            elif self._failure_count >= self._failure_threshold:
                self._state = "open"
                fire = True
        if fire:
            self._fire_on_open()

    def time_until_retry(self) -> float:
        with self._lock:
            if self._state != "open":
                return 0.0
            return max(0.0, self._current_recovery_timeout - (time.monotonic() - self._last_failure_time))

    def _fire_on_open(self) -> None:
        logger.warning("Circuit '%s' open: failures=%d, retry_in=%.1fs",
                       self.name, self._failure_count, self._current_recovery_timeout)
        if not self._on_open:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._on_open(self._failure_count, self._current_recovery_timeout))
        except RuntimeError:
            pass


class _CircuitRegistry:
    """Single named breaker per (name) across the entire process."""
    _registry: dict[str, CircuitBreaker] = {}

    @classmethod
    def get(cls, name: str, **kwargs) -> CircuitBreaker:
        if name not in cls._registry:
            cls._registry[name] = CircuitBreaker(name=name, **kwargs)
        return cls._registry[name]

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()


def with_circuit_breaker(
    *, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Wrap an async function with a named circuit breaker."""
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            cb = _CircuitRegistry.get(
                name, failure_threshold=failure_threshold, recovery_timeout=recovery_timeout,
            )
            if not cb.allow_request():
                raise CircuitOpen(name=name, retry_after_seconds=cb.time_until_retry())
            try:
                result = await fn(*args, **kwargs)
                cb.record_success()
                return result
            except Exception:
                cb.record_failure()
                raise
        return wrapper
    return decorator
