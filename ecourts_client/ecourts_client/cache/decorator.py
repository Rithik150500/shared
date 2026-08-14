"""``with_cache_sync`` -- Redis read-through decorator for picker/list methods.

Design invariants (see docs/superpowers/specs/2026-05-27-ecourts-picker-cache-design.md):
  * No backend set -> transparent pass-through (casepilot, most tests).
  * Cache hit -> deserialize and return, inner fn NOT called.
  * Cache miss -> live fetch; cache ONLY non-empty results (never poison the
    cache with a transient-empty 200).
  * Every Redis interaction fails OPEN: a GET/SETEX error, a corrupt entry, or a
    key-bind failure logs and serves the live result. The cache is an
    optimization, never a correctness gate.

Applied to the resilience-wrapped method (which carries ``__wrapped__`` = the raw
method) so ``inspect.signature`` still resolves the real parameter names for key
extraction.
"""
from __future__ import annotations

import inspect
import logging
from functools import wraps
from typing import Any, Callable, Type

from ecourts_client.cache.backend import get_backend
from ecourts_client.cache.keys import build_key
from ecourts_client.cache.serializer import from_json, to_json

logger = logging.getLogger(__name__)


def with_cache_sync(*, item_cls: Type[Any], key_args: list[str], ttl_seconds: int):
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            backend = get_backend()
            if backend is None:
                return fn(self, *args, **kwargs)

            try:
                bound = sig.bind(self, *args, **kwargs)
                bound.apply_defaults()
                key_vals = [bound.arguments[name] for name in key_args]
            except Exception:
                logger.exception("cache key bind failed for %s; bypassing", fn.__name__)
                return fn(self, *args, **kwargs)

            scope = getattr(self, "scope", "?")
            key = build_key(fn.__name__, scope, key_vals)

            try:
                cached = backend.get(key)
                if cached is not None:
                    return from_json(
                        cached.decode() if isinstance(cached, bytes) else cached,
                        item_cls,
                    )
            except Exception:
                logger.warning("cache GET failed for %s; falling through", key, exc_info=True)

            result = fn(self, *args, **kwargs)

            if result:  # cache non-empty results only
                try:
                    # SET key value EX ttl (not the deprecated SETEX command).
                    backend.set(key, to_json(result), ex=ttl_seconds)
                except Exception:
                    logger.warning(
                        "cache SET failed for %s; returning live result", key, exc_info=True
                    )

            return result

        setattr(wrapper, "_ecourts_cache_applied", True)
        return wrapper

    return decorator
