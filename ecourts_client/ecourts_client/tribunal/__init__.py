"""Tribunal forum-family adapters.

One generic ``Forum.TRIBUNAL`` forum, sub-typed by ``TribunalKind``. Each kind is
a ``ForumAdapter`` under ``tribunal/kinds/<kind>.py`` registered per-kind via
``register_adapter(Forum.TRIBUNAL, <KindClient>, kind=TribunalKind.<KIND>)`` in
``ecourts_client/__init__.py``. Wave-0 (no India proxy, mostly captcha-free)
ships NCLAT first; see ``docs/spike-tribunal-transport.md`` for the verified
transport contracts.
"""
from ecourts_client.tribunal.kinds.nclat import NCLATClient

__all__ = ["NCLATClient"]
