"""Tier-2 distributed rate limiter: one shared Redis GCRA schedule caps the
AGGREGATE eCourts egress rate across all processes on the single prod IP.

Injected behind the _get_rate_gate() factory (ECOURTS_USE_REDIS_LIMITER); exposes
the same wait() contract as _session._RateGate. FAIL-OPEN: any Redis problem
degrades to the per-process _RateGate floor, never worse than today.
"""
from __future__ import annotations

import os
import time
from typing import Any

# GCRA reservation: read the current interval (or base), reserve the next slot on
# the shared schedule using the Redis server clock (uniform across processes),
# return the delay-ms the caller must sleep. next_allowed gets a 60s PX so a stale
# far-future slot cannot wedge every process.
_WAIT_LUA = """
local base = tonumber(ARGV[1])
local interval = tonumber(redis.call('GET', KEYS[2])) or base
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1])) or 0
local slot = now
if nxt > now then slot = nxt end
redis.call('SET', KEYS[1], slot + interval * 1000, 'PX', 60000)
local delay = slot - now
if delay < 0 then delay = 0 end
return delay
"""

# AIMD widen: interval = min(current_or_base * factor, max), with a TTL so it
# auto-resets to base when the throttle clears (no per-success decay -> no race).
_PENALIZE_LUA = """
local base = tonumber(ARGV[1])
local factor = tonumber(ARGV[2])
local maxi = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local cur = tonumber(redis.call('GET', KEYS[1])) or base
local new = cur * factor
if new > maxi then new = maxi end
redis.call('SET', KEYS[1], new, 'EX', ttl)
return tostring(new)
"""

_NEXT_KEY = "ecourts:egress:v1:next_allowed"
_INTERVAL_KEY = "ecourts:egress:v1:interval"


class RedisRateLimiter:
    def __init__(
        self,
        base_interval: float,
        *,
        widen_factor: float,
        max_interval: float,
        penalty_ttl_seconds: int,
        sleep=time.sleep,
    ) -> None:
        self.base_interval = base_interval
        self.widen_factor = widen_factor
        self.max_interval = max_interval
        self.penalty_ttl_seconds = penalty_ttl_seconds
        self._sleep = sleep
        # Lazy singletons; _local is the fail-open floor (import here avoids a cycle).
        from ecourts_client._session import _RateGate
        self._local = _RateGate(base_interval, sleep=sleep)
        self._client: Any = None
        self._wait_script: Any = None
        self._penalize_script: Any = None

    def _redis(self) -> Any:
        """Lazy best-effort client + registered scripts. Mirrors
        _session._throttle_redis_client (lazy import, short timeouts, singleton)."""
        if self._client is not None:
            return self._client
        url = os.environ.get("SHARED_REDIS_URL") or os.environ.get("REDIS_URL")
        if not url:
            return None
        import redis  # lazy: casepilot may lack the wheel -> ImportError -> fail-open

        client = redis.Redis.from_url(url, socket_connect_timeout=0.25, socket_timeout=0.25)
        self._wait_script = client.register_script(_WAIT_LUA)      # EVALSHA + NOSCRIPT fallback
        self._penalize_script = client.register_script(_PENALIZE_LUA)
        self._client = client
        return self._client

    def _reserve_slot_ms(self) -> int:
        """Reserve the next shared slot; return the delay in ms. Raises on Redis
        error (caller handles fail-open). Split out for test observability."""
        self._redis()
        return int(self._wait_script(keys=[_NEXT_KEY, _INTERVAL_KEY], args=[self.base_interval]))

    def wait(self) -> None:
        try:
            if self._redis() is None:
                self._local.wait()
                return
            delay_ms = self._reserve_slot_ms()
        except Exception:  # noqa: BLE001 -- fail-open: never break transport
            self._local.wait()
            return
        if delay_ms > 0:
            self._sleep(delay_ms / 1000.0)

    def penalize(self) -> None:
        """Widen the shared interval after a 405 so ALL processes back off. Best
        effort; a Redis problem is a silent no-op (the throttle self-clears anyway)."""
        try:
            if self._redis() is None:
                return
            self._penalize_script(
                keys=[_INTERVAL_KEY],
                args=[self.base_interval, self.widen_factor, self.max_interval, self.penalty_ttl_seconds],
            )
        except Exception:  # noqa: BLE001
            pass
