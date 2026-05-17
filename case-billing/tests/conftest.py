"""Shared test fixtures for case-billing.

Registers the SQLite ↔ Python UUID adapter required by the in-memory
DB-backed tests (Subscription rows in test_pricing.py): the Postgres
``UUID`` columns use ``.with_variant(String(36), "sqlite")`` for DDL
compatibility, but the bind-parameter coercion still has to be set up
at the sqlite3 driver layer.
"""

from __future__ import annotations

import sqlite3
import uuid

# Adapter registration is process-wide and harmless for any non-SQLite
# backend (Postgres uses its own native UUID type). Mirrors the same
# fixup applied by data-access's own conftest.
sqlite3.register_adapter(uuid.UUID, str)
