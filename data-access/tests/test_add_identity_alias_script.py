from __future__ import annotations

import importlib.util
import pathlib

from data_access.daos import user_dao

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "add_identity_alias.py"


def _load():
    spec = importlib.util.spec_from_file_location("add_identity_alias", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_adds_verified_alias(db_session):
    mod = _load()
    owner, _ = user_dao.get_or_create_by_phone(db_session, phone="+919953652710")
    db_session.flush()
    row = mod.run(
        db_session, user_id=owner.id, kind="phone", value="8882271502",
        verified=True, added_by="op:test", reclaim=True,
    )
    assert row is not None and row.value == "+918882271502"
    routed, created = user_dao.get_or_create_by_phone(db_session, phone="8882271502")
    assert created is False and routed.id == owner.id
    # Spec §C: the add was audited.
    from data_access.models import AuditLog
    assert any(a.event_type == "identity.alias_added" for a in db_session.query(AuditLog).all())
