"""Health monitor polls bootstrap endpoint; tracks healthy/unhealthy transitions."""
from __future__ import annotations

import asyncio
import pytest

from ecourts_client.resilience.health_monitor import HealthMonitor


class _FakeProbe:
    def __init__(self, fail_after: int = 0):
        self.calls = 0
        self.fail_after = fail_after

    async def __call__(self) -> bool:
        self.calls += 1
        if self.fail_after and self.calls > self.fail_after:
            return False
        return True


@pytest.mark.asyncio
async def test_health_monitor_starts_healthy_after_success():
    probe = _FakeProbe()
    m = HealthMonitor(probe=probe, poll_interval=0.02, failure_threshold=2)
    await m.start()
    await asyncio.sleep(0.05)
    assert m.healthy is True
    await m.stop()


@pytest.mark.asyncio
async def test_health_monitor_flips_to_unhealthy_after_threshold():
    probe = _FakeProbe(fail_after=1)
    m = HealthMonitor(probe=probe, poll_interval=0.01, failure_threshold=2)
    await m.start()
    await asyncio.sleep(0.1)
    assert m.healthy is False
    await m.stop()
