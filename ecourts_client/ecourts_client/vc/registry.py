from __future__ import annotations
import logging
from collections.abc import Iterable
from ecourts_client.vc.models import VCAccess, VCRoomKey
from ecourts_client.vc.provider import VCLinkProvider

log = logging.getLogger(__name__)
_PROVIDERS: dict[str, VCLinkProvider] = {}


def register_vc_provider(name: str, provider: VCLinkProvider) -> None:
    _PROVIDERS[name] = provider


def get_vc_provider(name: str) -> VCLinkProvider | None:
    return _PROVIDERS.get(name)


def resolve_vc(key: VCRoomKey, *, providers: Iterable[VCLinkProvider] | None = None) -> VCAccess | None:
    """First provider to return non-None wins. Defaults to all registered.
    A provider that raises is swallowed (a bad map must not break indexing)."""
    for p in (providers if providers is not None else _PROVIDERS.values()):
        try:
            hit = p.resolve(key)
        except Exception as e:  # noqa: BLE001 - never let VC resolution break the caller
            log.warning("vc provider %r raised on %r: %s", p, key, e)
            continue
        if hit is not None:
            return hit
    return None
