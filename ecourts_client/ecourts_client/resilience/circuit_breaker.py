"""Layer-2 resilience: 3-state circuit breaker (closed -> open -> half_open -> closed).

Consolidates Munshi's internal pattern and Nowlez's `backend.scraper_resilience.CircuitBreaker`.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from ecourts_client.errors import CircuitOpen
from ecourts_client.resilience.court_key import UNKNOWN_KEY, is_court_scoped
from ecourts_client.resilience.failure_policy import Outcome, classify_failure


T = TypeVar("T")
logger = logging.getLogger(__name__)

# Ceiling on the probe-failure counter. ``2 ** _probe_failures`` is evaluated
# while holding the lock, and the counter only ever grows (it is reset solely by
# a SUCCESSFUL half-open probe), so a permanently-dead breaker would compute an
# ever-larger bignum on every failure. By 16 the min() against any sane recovery
# ceiling has saturated many times over, so nothing above it can change the
# result -- capping the counter (rather than the exponent expression) keeps the
# bound directly observable in a test.
_MAX_PROBE_EXPONENT = 16


def _court_breaker(policy, args, kwargs, global_name: str):
    """Resolve the per-court breaker for this call, or None."""
    if policy is None:
        return None, global_name
    key = policy.key_for(args, kwargs)
    # UNKNOWN_KEY is not a court. Giving it court semantics (a 300s window in
    # which successes deliberately do not heal) turned the shared bucket into a
    # second, MORE trigger-happy global breaker -- every hint-less fetch_pdf
    # lands there. Route it to the global breaker instead.
    if key == global_name or key == UNKNOWN_KEY:
        return None, global_name
    return _CircuitRegistry.get(
        key,
        failure_threshold=policy.failure_threshold,
        recovery_timeout=policy.recovery_timeout,
        failure_window_seconds=policy.failure_window_seconds,
        clock=getattr(policy, "clock", time.monotonic),
        max_recovery_timeout=getattr(policy, "max_recovery_timeout", 1800.0),
        jitter=getattr(policy, "jitter", 0.0),
    ), key


def _count_tripped_courts() -> int:
    """Courts currently considered DOWN, without mutating any breaker.

    Two bugs this avoids: reading the `.state` property TRANSITIONS
    open -> half_open (and hands out the probe token), and counting only
    literal "open" undercounts -- a court whose recovery timer elapsed stops
    being counted, so the census saturated and the guard was inert for any
    outage whose onset was spread over more than one recovery_timeout.
    """
    return sum(
        1 for name, cb in _CircuitRegistry.all_items()
        if is_court_scoped(name) and cb.peek_state() in ("open", "half_open")
    )


def _maybe_cascade(global_cb: "CircuitBreaker", policy) -> None:
    """Force the global breaker open when too many courts are down at once.

    Naive per-court keying makes a BROAD outage worse: N independent breakers
    each burn their own failure budget and then probe on their own half-open
    ladder, RAISING traffic against a host that bans by IP. Collapsing onto one
    breaker restores today's single genuinely valuable property.
    """
    threshold = getattr(policy, "cascade_open_threshold", 0)
    if not threshold:
        return
    open_courts = _count_tripped_courts()
    if open_courts >= threshold:
        logger.warning(
            "ECOURTS_CASCADE open_courts=%d threshold=%d -- forcing '%s' open",
            open_courts, threshold, global_cb.name,
        )
        global_cb.force_open()


def _run_outcome(exc, global_cb, court_cb, use_taxonomy, policy, gen=None, court_gen=None) -> None:
    """Route a failure to the right breaker(s) per the taxonomy."""
    if not use_taxonomy:
        global_cb.record_failure(gen)       # historical: everything is global
        return
    outcome = classify_failure(exc)
    if outcome is Outcome.NEUTRAL:
        # No availability signal. Deliberately NOT record_success() either --
        # healing here would let an interleaved neutral stream mask an outage.
        return
    if outcome is Outcome.TRIP_GLOBAL or court_cb is None:
        global_cb.record_failure(gen)
        return
    court_cb.record_failure(court_gen)
    # Only scan the registry when this court actually ended up down.
    if court_cb.peek_state() in ("open", "half_open"):
        _maybe_cascade(global_cb, policy)


def _acquire_gates(cb, per_court, args, kwargs, name):
    """Admission control for one call. SINGLE implementation shared by the async
    and sync decorators -- they were hand-copies, and mutation testing showed a
    defect injected into one of them survived the whole suite.

    Returns (court_cb, gen, court_gen) or raises CircuitOpen.
    """
    court_cb, court_name = _court_breaker(per_court, args, kwargs, name)
    # COURT GATE FIRST. Consulting the global first consumed its half-open
    # token, and the court gate then rejected before any call was made -- so a
    # single down court repeatedly stole the global's recovery probe and
    # starved every healthy court.
    court_gen = None
    if court_cb is not None:
        court_ok, court_gen = court_cb.try_acquire()
        if not court_ok:
            raise CircuitOpen(
                name=court_name, retry_after_seconds=court_cb.time_until_retry()
            )
    allowed, gen = cb.try_acquire()
    if not allowed:
        if court_cb is not None:
            court_cb.return_probe()   # abandoning: hand the token back
        raise CircuitOpen(name=name, retry_after_seconds=cb.time_until_retry())
    return court_cb, gen, court_gen


def _record_success(global_cb, court_cb, gen=None, court_gen=None) -> None:
    """Shared success path for both decorators (they were hand-copied)."""
    global_cb.record_success(gen)
    if court_cb is not None:
        court_cb.record_success(court_gen)


class CircuitBreaker:
    """3-state circuit breaker.

    Args:
        name: identifier for metrics/logs.
        failure_threshold: consecutive failures before opening.
        recovery_timeout: seconds before transitioning open -> half_open.
        max_recovery_timeout: ceiling on exponential back-off in half_open re-open.
        on_open: optional async callback (failure_count, recovery_timeout) invoked once per open transition.
        failure_window_seconds: when set, count failures in a SLIDING WINDOW
            instead of consecutively. Consecutive counting cannot trip a
            coarse (state-level) key, because a partial outage produces an
            interleaved success/failure stream that never reaches N in a row.
            Leave None for the historical consecutive semantics.
        clock: injectable monotonic clock, for deterministic tests. Patching
            ``time.monotonic`` globally is unsafe -- asyncio shares it.
    """
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        max_recovery_timeout: float = 1800.0,
        on_open: Callable[[int, float], Awaitable[None]] | None = None,
        failure_window_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: float = 0.0,
        rng: Callable[[], float] = random.random,
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
        self._window = failure_window_seconds
        self._clock = clock
        self._failures: deque[float] = deque()
        # Fraction by which a recovery window may be shortened at random.
        # 0.0 (the default) keeps the ladder exactly deterministic, which every
        # test that constructs a CircuitBreaker directly relies on.
        self._jitter = max(0.0, min(1.0, jitter))
        self._rng = rng
        # Bumped on every transition INTO open. A call admitted in an earlier
        # epoch must not record its outcome against the current one.
        self._generation = 0

    def _backoff(self, nominal: float) -> float:
        """Apply DOWNWARD-ONLY jitter to a recovery window.

        Downward-only rather than symmetric for two reasons: the result can never
        exceed ``_max_recovery_timeout``, so the ceiling stays a hard ceiling; and
        it can never lengthen a caller's wait beyond the nominal ladder.

        The point is decorrelation, not the size of the change. A deterministic
        ladder means a caller on a fixed cadence arrives on the re-arm instant
        every single time and wins the one half-open probe on every rung -- which
        is exactly how a 60s-interval cron walked the shared breaker from 60s to
        480s on prod and held it open for interactive users. It also stops N
        breakers that trip in one cascade from re-probing in lockstep.
        """
        if self._jitter <= 0.0:
            return nominal
        return nominal * (1.0 - self._jitter * self._rng())

    @property
    def state(self) -> str:
        with self._lock:
            now = self._clock()
            if self._state == "open" and (now - self._last_failure_time) >= self._current_recovery_timeout:
                self._state = "half_open"
                self._half_open_allowed = True
                self._half_open_time = now
            elif self._state == "half_open" and not self._half_open_allowed:
                if (now - self._half_open_time) >= self._base_recovery_timeout:
                    self._half_open_allowed = True
                    self._half_open_time = now
            return self._state

    def peek_state(self) -> str:
        """The state a reader would observe, WITHOUT mutating anything.

        `state` transitions open -> half_open as a side effect (and arms the
        probe token). Any code that merely *inspects* a breaker -- the cascade
        census, metrics, debug endpoints -- must use this instead.
        """
        with self._lock:
            now = self._clock()
            if self._state == "open" and (now - self._last_failure_time) >= self._current_recovery_timeout:
                return "half_open"
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def try_acquire(self) -> tuple[bool, int]:
        """allow_request() plus the epoch observed in the SAME critical section.

        Returning the generation atomically is what makes fencing sound: reading
        it afterwards would race with another thread opening the breaker.
        """
        with self._lock:
            now = self._clock()
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
                return True, self._generation
            if s == "half_open" and self._half_open_allowed:
                self._half_open_allowed = False
                return True, self._generation
            return False, self._generation

    def return_probe(self) -> None:
        """Credit back a half-open token for a call abandoned before it ran.

        Without this, a gate that rejects AFTER another gate has consumed its
        token silently costs that breaker a full recovery cycle.
        """
        with self._lock:
            if self._state == "half_open":
                self._half_open_allowed = True

    def allow_request(self) -> bool:
        """Back-compat wrapper; prefer try_acquire() so outcomes can be fenced."""
        return self.try_acquire()[0]

    def record_success(self, generation: int | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # stale epoch: this call was admitted before the breaker opened
            if self._state == "half_open":
                # A successful probe means recovery: reset the back-off ladder
                # AND clear the window, else stale failures re-open instantly.
                self._probe_failures = 0
                self._current_recovery_timeout = self._base_recovery_timeout
                self._failures.clear()
                self._failure_count = 0
                self._state = "closed"
                self._half_open_allowed = False
                return
            if self._window is not None:
                # Windowed mode: a success while CLOSED is NOT healing.
                # Failures must age out by time, not be erased by traffic, or a
                # coarse key can never accumulate enough to trip.
                return
            self._failure_count = 0
            self._state = "closed"
            self._half_open_allowed = False

    def record_failure(self, generation: int | None = None) -> None:
        fire = False
        snap_failure_count = 0
        snap_recovery_timeout = 0.0
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # stale epoch
            now = self._clock()
            self._failure_count += 1
            self._last_failure_time = now
            if self._window is not None:
                self._failures.append(now)
                cutoff = now - self._window
                while self._failures and self._failures[0] < cutoff:
                    self._failures.popleft()
                self._failure_count = len(self._failures)
            if self._state == "half_open":
                self._probe_failures = min(
                    self._probe_failures + 1, _MAX_PROBE_EXPONENT
                )
                self._current_recovery_timeout = self._backoff(min(
                    self._base_recovery_timeout * (2 ** self._probe_failures),
                    self._max_recovery_timeout,
                ))
                self._state = "open"
                self._generation += 1
                fire = True
            elif self._failure_count >= self._failure_threshold:
                # Jitter the FIRST open too. Without this, every breaker tripped
                # by one broad outage re-arms at an identical instant and the
                # herd probes in lockstep. (In the closed state _current is
                # always the base -- the ladder only climbs in half_open, and
                # record_success resets it -- so this is the base plus jitter.)
                self._current_recovery_timeout = self._backoff(
                    self._base_recovery_timeout
                )
                self._state = "open"
                self._generation += 1
                fire = True
            if fire:
                snap_failure_count = self._failure_count
                snap_recovery_timeout = self._current_recovery_timeout
        if fire:
            self._fire_on_open(snap_failure_count, snap_recovery_timeout)

    def force_open(self) -> None:
        """Open the breaker regardless of the failure count.

        Used by the cascade guard: when many per-court breakers are open the
        host is hostile, and N independent half-open ladders would RAISE
        traffic against an IP that bans on burst. Collapsing onto the global
        breaker restores the "a broad outage silences the process" property.
        """
        fire = False
        with self._lock:
            if self._state != "open":
                self._state = "open"
                self._half_open_allowed = False
                self._generation += 1
                fire = True
            self._last_failure_time = self._clock()
            snap_count, snap_timeout = self._failure_count, self._current_recovery_timeout
        if fire:
            self._fire_on_open(snap_count, snap_timeout)

    def time_until_retry(self) -> float:
        with self._lock:
            if self._state == "half_open" and not self._half_open_allowed:
                # A probe is out (or was consumed): the next grant is one base
                # recovery away. Reporting 0.0 here made callers hot-loop.
                return max(0.0, self._base_recovery_timeout - (self._clock() - self._half_open_time))
            if self._state != "open":
                return 0.0
            return max(0.0, self._current_recovery_timeout - (self._clock() - self._last_failure_time))

    def _fire_on_open(self, failure_count: int, recovery_timeout: float) -> None:
        logger.warning("Circuit '%s' open: failures=%d, retry_in=%.1fs",
                       self.name, failure_count, recovery_timeout)
        if not self._on_open:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._on_open(failure_count, recovery_timeout))
        except RuntimeError:
            pass


class _CircuitRegistry:
    """Single named breaker per (name) across the entire process."""
    _registry: dict[str, CircuitBreaker] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, name: str, **kwargs) -> CircuitBreaker:
        with cls._lock:
            if name not in cls._registry:
                cls._registry[name] = CircuitBreaker(name=name, **kwargs)
            return cls._registry[name]

    @classmethod
    def all_items(cls) -> list[tuple[str, "CircuitBreaker"]]:
        """Snapshot of (name, breaker). Used by the cascade guard."""
        with cls._lock:
            return list(cls._registry.items())

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._registry.clear()


def with_circuit_breaker(
    *, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0,
    use_taxonomy: bool = False, per_court=None,
    max_recovery_timeout: float = 1800.0, jitter: float = 0.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Wrap an async function with a named circuit breaker.

    Args:
        use_taxonomy: when True, only exceptions that the failure taxonomy
            classifies as availability signals count toward opening. Default
            False preserves the historical count-everything behaviour.
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            cb = _CircuitRegistry.get(
                name, failure_threshold=failure_threshold, recovery_timeout=recovery_timeout,
                max_recovery_timeout=max_recovery_timeout, jitter=jitter,
            )
            court_cb, gen, court_gen = _acquire_gates(
                cb, per_court, args, kwargs, name
            )
            try:
                result = await fn(*args, **kwargs)
                _record_success(cb, court_cb, gen, court_gen)
                return result
            except Exception as exc:
                _run_outcome(exc, cb, court_cb, use_taxonomy, per_court, gen, court_gen)
                raise
        return wrapper
    return decorator


def with_circuit_breaker_sync(
    *, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0,
    use_taxonomy: bool = False, per_court=None,
    max_recovery_timeout: float = 1800.0, jitter: float = 0.0,
):
    """Sync mirror of `with_circuit_breaker`. Uses the SAME named registry
    as the async wrapper, so sync and async callers see one shared breaker
    per name. Thread-safe by virtue of CircuitBreaker._lock added in Task 1.

    See `with_circuit_breaker` for `use_taxonomy`.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            cb = _CircuitRegistry.get(
                name, failure_threshold=failure_threshold, recovery_timeout=recovery_timeout,
                max_recovery_timeout=max_recovery_timeout, jitter=jitter,
            )
            court_cb, gen, court_gen = _acquire_gates(
                cb, per_court, args, kwargs, name
            )
            try:
                result = fn(*args, **kwargs)
                _record_success(cb, court_cb, gen, court_gen)
                return result
            except Exception as exc:
                _run_outcome(exc, cb, court_cb, use_taxonomy, per_court, gen, court_gen)
                raise
        return wrapper
    return decorator
