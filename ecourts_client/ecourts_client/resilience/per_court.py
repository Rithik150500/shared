"""Policy object for per-court circuit-breaker keying.

Kept separate from ``circuit_breaker`` so the decorator signatures stay legible
and so a caller can build the policy once from config instead of threading five
keyword arguments through every call site.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ecourts_client.resilience.court_key import UNKNOWN_KEY


@dataclass(frozen=True)
class PerCourtPolicy:
    """How to derive and run a per-court breaker alongside the global one.

    Args:
        key_fn: maps the wrapped call's arguments to a court key. MUST be total
            -- see ``court_key.court_key_for_cnr``. Any exception is swallowed
            and treated as ``UNKNOWN_KEY``, because a key-derivation bug must
            never break a court fetch.
        failure_threshold: failures (within the window) before a court opens.
        recovery_timeout: base open -> half_open delay for a court breaker.
            Longer than the global default: a court that is genuinely down
            stays down for a while, and probing it costs the shared IP budget.
        failure_window_seconds: sliding window for court failures. Required --
            consecutive counting cannot trip a coarse, state-level key.
        cascade_open_threshold: when at least this many court breakers are open
            at once, force the global breaker open so the process goes quiet.
            0 disables the guard.
    """

    key_fn: Callable[..., str]
    failure_threshold: int = 5
    recovery_timeout: float = 120.0
    failure_window_seconds: float = 300.0
    cascade_open_threshold: int = 8
    #: Injectable monotonic clock for the court breakers, so a staggered
    #: outage can be simulated deterministically in tests.
    clock: Callable[[], float] = field(default=time.monotonic, compare=False)

    def key_for(self, args: tuple, kwargs: dict) -> str:
        """Derive the court key, never raising."""
        try:
            return self.key_fn(*args, **kwargs) or UNKNOWN_KEY
        except Exception:  # noqa: BLE001 - a keying bug must not break the fetch
            return UNKNOWN_KEY
