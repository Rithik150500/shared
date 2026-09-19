"""Process-lifetime cache backend. The consumer owns the Redis client and
injects it once via :func:`set_backend`; the decorator duck-types ``.get`` /
``.setex`` on it (no hard ``redis`` dependency in this package)."""
from __future__ import annotations

from typing import Any, Optional

_backend: Optional[Any] = None  # a redis.Redis-compatible client


def set_backend(client: Any) -> None:
    global _backend
    _backend = client


def get_backend() -> Optional[Any]:
    return _backend


def clear_backend() -> None:
    """Test helper -- reset to the no-cache (pass-through) state."""
    global _backend
    _backend = None
