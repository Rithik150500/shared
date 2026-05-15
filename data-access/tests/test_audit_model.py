import uuid
from data_access.models.audit import AuditLog


def test_audit_log_table_name():
    assert AuditLog.__tablename__ == "audit_log"


def test_audit_log_metadata_column_exists():
    # SQL column is 'metadata' even though Python attr is 'metadata_' (avoids
    # collision with DeclarativeBase.metadata).
    assert "metadata" in AuditLog.__table__.c


def test_audit_log_source_check_constraint():
    constraints = [c for c in AuditLog.__table__.constraints if c.__class__.__name__ == "CheckConstraint"]
    names = {c.name for c in constraints}
    assert "audit_source_check" in names


def test_audit_log_user_id_fk_set_null():
    fks = list(AuditLog.__table__.c["user_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"
    assert fks[0].target_fullname == "users.id"


def test_audit_log_actor_id_fk_set_null():
    fks = list(AuditLog.__table__.c["actor_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"
