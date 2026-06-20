"""Regression tests for the phone-format identity split.

A user provisioned on the web with a bare 10-digit phone (`9953652710`) must be
the SAME row the WhatsApp webhook resolves when it normalizes to `+919953652710`.
Before the normalizer, `UNIQUE(phone)` saw two keys and `get_or_create_by_phone`
spun up a second, empty user row → "portfolio is empty".
"""
from data_access.daos import user_dao


def test_get_or_create_stores_canonical_phone(db_session):
    user, was_created = user_dao.get_or_create_by_phone(db_session, phone="9953652710")
    assert was_created is True
    assert user.phone == "+919953652710"


def test_get_or_create_dedupes_across_phone_formats(db_session):
    # Web provisioning created the row as a bare 10-digit number...
    u1, created1 = user_dao.get_or_create_by_phone(db_session, phone="9953652710")
    # ...the webhook later resolves the same person as E.164.
    u2, created2 = user_dao.get_or_create_by_phone(db_session, phone="+919953652710")
    assert created1 is True
    assert created2 is False
    assert u1.id == u2.id


def test_get_by_phone_matches_across_formats(db_session):
    created, _ = user_dao.get_or_create_by_phone(db_session, phone="+919953652710")
    assert user_dao.get_by_phone(db_session, "9953652710").id == created.id
    assert user_dao.get_by_phone(db_session, "919953652710").id == created.id
    assert user_dao.get_by_phone(db_session, "+91 99536-52710").id == created.id
