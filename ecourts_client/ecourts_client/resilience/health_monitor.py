"""Layer-4 supporting service: periodic probe of eCourts bootstrap endpoint."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable


logger = logging.getLogger(__name__)


class HealthMonitor:
    """Periodically probe eCourts via `appReleaseWebService.php` bootstrap.

    `healthy` flips False after `failure_threshold` consecutive failed probes;
    flips True on the next successful probe.
    """
    def __init__(
        self,
        *,
        probe: Callable[[], Awaitable[bool]],
        poll_interval: float = 30.0,
        failure_threshold: int = 3,
    ) -> None:
        self._probe = probe
        self._poll_interval = poll_interval
        self._failure_threshold = failure_threshold
        self._consecutive_failures = 0
        self._healthy = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ok = await self._probe()
            except Exception as e:
                logger.warning("health probe raised: %s", e)
                ok = False
            if ok:
                self._consecutive_failures = 0
                self._healthy = True
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    self._healthy = False
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
