"""Consumer-forum-specific value objects.

The case-level data reuses the shared generic ``Case``/``Party``/``OrderRef``
(``ecourts_client.models``); only the commission hierarchy is Consumer-specific.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommissionRef:
    """A consumer commission (NCDRC / State / District / circuit bench).

    ``commission_id`` is e-Jagriti's 8-digit numeric id used to scope a case
    search (e.g. 11290000=Karnataka SCDRC, 11290525=Bangalore Urban DCDRC).
    ``is_bench`` flags circuit/additional benches (``circuitAdditionBenchStatus``).
    """

    commission_id: int
    name: str
    is_bench: bool = False
    active: bool = True
