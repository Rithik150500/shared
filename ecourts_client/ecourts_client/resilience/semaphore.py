"""Layer-1 resilience: bound concurrent in-flight eCourts calls.

Uses threading.BoundedSemaphore as the primitive so both sync and async
callers can share the same named instance. The async wrapper acquires
the semaphore in a worker thread via asyncio.to_thread to avoid
blocking the event loop.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar


T = TypeVar("T")


class _SemaphoreRegistry:
    """Single named semaphore per (name) across the entire process."""
    _registry: dict[str, threading.BoundedSemaphore] = {}
    _waiting: dict[str, int] = {}
    _waiting_lock = threading.Lock()
    _registry_lock = threading.Lock()

    @classmethod
    def get(cls, name: str, max_concurrency: int = 10) -> threading.BoundedSemaphore:
        with cls._registry_lock:
            if name not in cls._registry:
                cls._registry[name] = threading.BoundedSemaphore(max_concurrency)
                cls._waiting[name] = 0
            return cls._registry[name]

    @classmethod
    def inc_waiting(cls, name: str) -> int:
        with cls._waiting_lock:
            cls._waiting[name] = cls._waiting.get(name, 0) + 1
            return cls._waiting[name]

    @classmethod
    def dec_waiting(cls, name: str) -> int:
        with cls._waiting_lock:
            cls._waiting[name] = max(0, cls._waiting.get(name, 0) - 1)
            return cls._waiting[name]

    @classmethod
    def reset(cls) -> None:
        with cls._registry_lock:
            cls._registry.clear()
        with cls._waiting_lock:
            cls._waiting.clear()


def with_semaphore(
    *, name: str, max_concurrency: int = 10,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cap concurrent in-flight ASYNC calls under the named semaphore.

    The underlying threading.BoundedSemaphore is acquired via
    asyncio.to_thread so the event loop is not blocked while waiting.
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            sem = _SemaphoreRegistry.get(name, max_concurrency=max_concurrency)
            _SemaphoreRegistry.inc_waiting(name)
            try:
                await asyncio.to_thread(sem.acquire)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    sem.release()
            finally:
                _SemaphoreRegistry.dec_waiting(name)
        return wrapper
    return decorator


def with_semaphore_sync(
    *, name: str, max_concurrency: int = 10,
):
    """Sync mirror of `with_semaphore`. Shares the SAME named
    BoundedSemaphore instance as the async wrapper via `_SemaphoreRegistry`,
    so a sync caller and an async caller compete for the same N permits.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            sem = _SemaphoreRegistry.get(name, max_concurrency=max_concurrency)
            _SemaphoreRegistry.inc_waiting(name)
            try:
                with sem:
                    return fn(*args, **kwargs)
            finally:
                _SemaphoreRegistry.dec_waiting(name)
        return wrapper
    return decorator
