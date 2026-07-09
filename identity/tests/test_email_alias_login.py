from __future__ import annotations

from data_access.daos import email_otp_dao, identity_alias_dao, user_dao
from identity import api as identity_api
from identity.api import _canonicalize_email
from identity.otp.issuer import hash_otp_code


def _make_verified_email_otp(session, email):
    return email_otp_dao.insert(
        session, email=_canonicalize_email(email), code_hash=hash_otp_code("424242")
    )


def test_email_alias_login_mints_for_owner_no_stepup(db_session):
    # Owner account with a phone + password (i.e. HAS a second factor) so the
    # legacy path would otherwise demand step-up for an unverified email.
    owner, _ = user_dao.get_or_create_by_phone(db_session, phone="+919953652710")
    user_dao.ensure_nowlez_extension(db_session, owner.id, name="Nitesh")
    user_dao.update_password(db_session, owner.id, "argon2id$dummy")
    identity_alias_dao.add_alias(
        db_session, user_id=owner.id, kind="email",
        value="nitishv245@gmail.com", verified=True,
    )
    db_session.flush()

    o = _make_verified_email_otp(db_session, "nitishv245@gmail.com")
    db_session.flush()
    out = identity_api.verify_email_otp_and_login(
        db_session, otp_id=o.id, code="424242", brand="nowlez", name="Nitesh"
    )
    # A successful mint (dict returned, no AccountLinkStepUpRequired raised)...
    assert set(out) == {"access_token", "refresh_token", "user"}
    # ...routed to the OWNER: no new account was minted, no primary email created.
    from data_access.models import User

    assert db_session.query(User).count() == 1
    assert user_dao.get_by_email(db_session, "nitishv245@gmail.com") is None
