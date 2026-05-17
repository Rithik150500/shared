"""Layer-1 resilience: bound concurrent in-flight eCourts calls."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar


T = TypeVar("T")


class _SemaphoreRegistry:
    """Single named semaphore per (name) across the entire process."""
    _registry: dict[str, asyncio.Semaphore] = {}
    _waiting: dict[str, int] = {}

    @classmethod
    def get(cls, name: str, max_concurrency: int = 10) -> asyncio.Semaphore:
        if name not in cls._registry:
            cls._registry[name] = asyncio.Semaphore(max_concurrency)
            cls._waiting[name] = 0
        return cls._registry[name]

    @classmethod
    def inc_waiting(cls, name: str) -> int:
        cls._waiting[name] = cls._waiting.get(name, 0) + 1
        return cls._waiting[name]

    @classmethod
    def dec_waiting(cls, name: str) -> int:
        cls._waiting[name] = max(0, cls._waiting.get(name, 0) - 1)
        return cls._waiting[name]

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()
        cls._waiting.clear()


def with_semaphore(
    *, name: str, max_concurrency: int = 10,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cap concurrent in-flight calls under the named semaphore."""
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            sem = _SemaphoreRegistry.get(name, max_concurrency=max_concurrency)
            _SemaphoreRegistry.inc_waiting(name)
            try:
                async with sem:
                    return await fn(*args, **kwargs)
            finally:
                _SemaphoreRegistry.dec_waiting(name)
        return wrapper
    return decorator
