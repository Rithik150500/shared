"""Redis read-through cache for the quasi-static eCourts picker/list methods.

A returning user (or a second user picking the same state) hits Redis instead of
eCourts -- and, because the cache sits OUTSIDE the resilience stack (see
``_resilience_apply``), a cache hit is served even while the shared circuit
breaker is open during an IP-wide throttle.

Wiring: the consumer calls :func:`set_backend` once at startup with a
redis.Redis-compatible client. With no backend set (casepilot, most tests) the
cache decorator is a transparent pass-through.
"""
from ecourts_client.cache.backend import clear_backend, get_backend, set_backend

__all__ = ["set_backend", "get_backend", "clear_backend"]
