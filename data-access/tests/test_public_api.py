"""Smoke test: package-level public API resolves all advertised exports."""


def test_top_level_imports():
    from data_access import (
        AuditLog,
        AuthSession,
        OtpCode,
        SessionFactory,
        User,
        UserMunshi,
        UserNowlez,
        daos,
        engine,
        get_session,
    )
    # Sanity check — these are real objects, not None
    # Note: `engine` is the submodule; instance lives at `engine.engine`.
    assert engine.__name__ == "data_access.engine"
    assert engine.engine is not None
    assert get_session is not None
    assert SessionFactory is not None
    assert User.__tablename__ == "users"
    assert UserMunshi.__tablename__ == "users_munshi"
    assert UserNowlez.__tablename__ == "users_nowlez"
    assert AuthSession.__tablename__ == "auth_sessions"
    assert OtpCode.__tablename__ == "otp_codes"
    assert AuditLog.__tablename__ == "audit_log"


def test_dao_module_exports():
    from data_access.daos import audit_dao, otp_dao, session_dao, user_dao
    # Each DAO module exposes at least one public function
    assert callable(user_dao.get_or_create_by_phone)
    assert callable(session_dao.create)
    assert callable(otp_dao.insert)
    assert callable(audit_dao.log_event)
