"""Shared data-access package — identity-only models in Phase 1.

The `engine` instance lives at `data_access.engine.engine` — not re-exported here,
because `from .engine import engine` would shadow the `data_access.engine` submodule
and break `import data_access.engine` for callers (and `importlib.reload`).
"""
from . import daos, engine
from .engine import SessionFactory, get_session
from .models import AuditLog, AuthSession, OtpCode, User, UserMunshi, UserNowlez

__version__ = "0.1.0"
__all__ = [
    "engine",          # the submodule; the instance is `data_access.engine.engine`
    "get_session",
    "SessionFactory",
    "User",
    "UserMunshi",
    "UserNowlez",
    "AuthSession",
    "OtpCode",
    "AuditLog",
    "daos",
]
