"""Self-contained tests for identity.attach_phone_with_otp.

Backs the sub-project D email->phone grace flow: a migrated email-only user
(phone=NULL) verifies a phone OTP and links the phone to their *existing*
account (no get-or-create / no duplicate). Uses in-memory SQLite so it runs
without a Postgres dependency (mirrors the data_access db_session fixture).
"""
from __future__ import annotations

import sqlite3
import uuid as _uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from data_access.base import Base
from data_access.daos import otp_dao
from data_access.models import AuditLog, AuthSession, OtpCode, User
from identity import attach_phone_with_otp
from identity.errors import OtpInvalid, PhoneInUse
from identity.otp.issuer import hash_otp_code

sqlite3.register_adapter(_uuid.UUID, str)


@pytest.fixture
def sess():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__, OtpCode.__table__,
            AuthSession.__table__, AuditLog.__table__,
        ],
    )
    s = sessionmaker(engine)()
    yield s
    s.close()


def _user(sess, *, email, phone=None):
    # password_hash carries a bcrypt string post-migration; attach never reads it.
    u = User(email=email, phone=phone, password_hash="$2b$12$legacybcrypthash")
    sess.add(u)
    sess.flush()
    return u


def _otp(sess, phone, *, code="123456"):
    return otp_dao.insert(sess, phone=phone, code_hash=hash_otp_code(code), channel="whatsapp")


def test_attach_links_phone_and_issues_tokens(sess):
    u = _user(sess, email="grace@x.in")
    o = _otp(sess, "+919800000001")

    res = attach_phone_with_otp(sess, user_id=u.id, otp_id=o.id, code="123456")

    assert res["access_token"] and res["refresh_token"]
    assert res["user"]["phone"] == "+919800000001"
    assert res["user"]["id"] == str(u.id)
    # phone persisted onto the SAME user — no duplicate account
    assert sess.get(User, u.id).phone == "+919800000001"
    assert sess.execute(select(func.count(User.id))).scalar_one() == 1
    # a refresh session + a "phone.linked" audit row were written
    assert sess.execute(
        select(AuthSession).where(AuthSession.user_id == u.id)
    ).first() is not None
    ev = sess.execute(
        select(AuditLog).where(AuditLog.event_type == "phone.linked")
    ).scalar_one()
    assert str(ev.user_id) == str(u.id) and ev.source == "nowlez"


def test_attach_accepts_str_ids(sess):
    u = _user(sess, email="grace@x.in")
    o = _otp(sess, "+919800000009")
    res = attach_phone_with_otp(sess, user_id=str(u.id), otp_id=str(o.id), code="123456")
    assert res["user"]["phone"] == "+919800000009"


def test_attach_rejects_phone_in_use(sess):
    _user(sess, email="owner@x.in", phone="+919800000002")
    grace = _user(sess, email="grace@x.in")
    o = _otp(sess, "+919800000002")
    with pytest.raises(PhoneInUse):
        attach_phone_with_otp(sess, user_id=grace.id, otp_id=o.id, code="123456")
    assert sess.get(User, grace.id).phone is None  # not attached


def test_attach_rejects_bad_code(sess):
    u = _user(sess, email="grace@x.in")
    o = _otp(sess, "+919800000003", code="111111")
    with pytest.raises(OtpInvalid):
        attach_phone_with_otp(sess, user_id=u.id, otp_id=o.id, code="999999")
    assert sess.get(User, u.id).phone is None
