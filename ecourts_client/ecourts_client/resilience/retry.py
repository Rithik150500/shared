"""Layer-3 retry with exponential backoff. Only retries transient transport errors."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from ecourts_client.errors import CourtSiteDown, RateLimited


T = TypeVar("T")
logger = logging.getLogger(__name__)

# Only these exception types are retried; everything else (CNRNotFound, CNRMalformed,
# BlockedByGeoIP, SchemaChanged, PDFNotFound, PDFInvalid, JWTExpired, CircuitOpen, etc.)
# propagates immediately.
_RETRIABLE = (CourtSiteDown,)


def with_retry(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry on `_RETRIABLE` exceptions; exponential backoff."""
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except _RETRIABLE as e:
                    last_exc = e
                    if attempt + 1 == max_attempts:
                        break
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.info("retry %d/%d after %s in %.2fs",
                                attempt + 1, max_attempts, type(e).__name__, delay)
                    await asyncio.sleep(delay)
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator
