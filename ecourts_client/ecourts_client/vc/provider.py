from __future__ import annotations
from typing import Protocol, runtime_checkable
from ecourts_client.vc.models import VCAccess, VCRoomKey


@runtime_checkable
class VCLinkProvider(Protocol):
    """Resolve a courtroom key to its VC access, or None on miss. Never raises."""
    def resolve(self, key: VCRoomKey) -> VCAccess | None: ...
