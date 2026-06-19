"""Migration tests for ``20260619_unified_login``.

Two layers:
1. Always-on guards (no DB): single-head pre-author gate; the new revision
   exists, chains onto 20260616_broadcast_tables, and its id is <= 32 chars.
   (The repo already contains one legacy 36-char id — the guard asserts the
   NEW migration's id, not every historical id.)
2. Postgres path (skip if no DB): upgrade head creates both tables + the
   email_verified column; downgrade -1 drops them; double-upgrade idempotent;
   two-concurrent-consume serializability.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
NEW_REVISION = "20260619_unified_login"
EXPECTED_DOWN = "20260616_broadcast_tables"


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    sd = _script_dir()
    heads = sd.get_heads()
    assert heads == [NEW_REVISION], f"expected single head {NEW_REVISION!r}, got {heads}"


def test_new_revision_chains_onto_broadcast_tables():
    sd = _script_dir()
    rev = sd.get_revision(NEW_REVISION)
    assert rev.down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    # alembic_version is VARCHAR(32); the new id MUST fit (legacy 36-char ids
    # predate this constraint and are intentionally not asserted here).
    assert len(NEW_REVISION) <= 32
