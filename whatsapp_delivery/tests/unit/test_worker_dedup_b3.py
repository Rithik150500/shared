"""B-3 worker-side dedup tests.

Covers the worker's ``dedup_per_day`` short-circuit:

1. First call → claim row inserted, Meta API hit, wamid returned.
2. Second call with same (user_id, template, today_ist) → claim
   short-circuits, no Meta API call, ``whatsapp_dedup_short_circuit_total``
   metric logged.
3. The short-circuit is gated on the template being in the
   ``_DEDUP_DAILY_TEMPLATES`` allowlist — a misconfigured
   ``dedup_per_day=True`` on a transactional template must still send.
4. ``dedup_per_day=False`` (default) preserves the legacy behavior on the
   daily-cadence templates as well.

Uses in-memory SQLite + monkey-patched ``data_access.engine.get_session``
so the claim INSERT lands in a controllable, isolated DB.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa: F401 — register models
from data_access.models import User

from whatsapp_delivery.dispatch import worker as w


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)


@pytest.fixture
def sqlite_session_factory(monkeypatch):
    """Patch ``data_access.engine.get_session`` so the worker's claim
    INSERT lands in an in-memory SQLite DB.

    Returns a sessionmaker the test can also use for assertions and
    pre-seeding (e.g. inserting users).
    """
    import sqlite3
    sqlite3.register_adapter(uuid.UUID, str)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_get_session():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # The worker imports get_session lazily inside _claim_daily_send_slot, so
    # we patch the canonical symbol once.
    import data_access.engine
    monkeypatch.setattr(data_access.engine, "get_session", fake_get_session)

    yield Session
    engine.dispose()


def _seed_user(Session, phone: str = "+919876543210") -> User:
    s = Session()
    try:
        u = User(phone=phone)
        s.add(u)
        s.commit()
        # Detach: the test needs the id outside the session.
        s.refresh(u)
        return u
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock
def test_first_call_claims_and_calls_meta(sqlite_session_factory):
    """The first send for (user, daily_template, today) makes the Meta call."""
    Session = sqlite_session_factory
    user = _seed_user(Session)

    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.first"}]})
    )

    wamid = w._do_send_template(
        to="+919876543210",
        template_name="nowlez_tomorrow_hearings_v1",
        language="en_US",
        variables={"count": "1", "formatted_list": "Mehta v. State"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=str(user.id),
        dedup_per_day=True,
    )
    assert wamid == "wamid.first"

    # Claim row was inserted by the worker.
    from data_access.models import WhatsAppDeliveryLog
    with Session() as s:
        rows = s.query(WhatsAppDeliveryLog).filter_by(
            user_id=user.id,
            template_name="nowlez_tomorrow_hearings_v1",
        ).all()
        assert len(rows) == 1
        assert rows[0].send_date_ist is not None


@respx.mock
def test_second_call_short_circuits_without_meta(
    sqlite_session_factory, caplog,
):
    """Two pods race the same daily send — the second one short-circuits."""
    Session = sqlite_session_factory
    user = _seed_user(Session)

    route = respx.post(
        "https://graph.facebook.com/v20.0/111/messages"
    ).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.first"}]})
    )

    common_kwargs = dict(
        to="+919876543210",
        template_name="nowlez_tomorrow_hearings_v1",
        language="en_US",
        variables={"count": "1", "formatted_list": "Mehta v. State"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=str(user.id),
        dedup_per_day=True,
    )

    # Pod A.
    first = w._do_send_template(**common_kwargs)
    assert first == "wamid.first"
    assert route.call_count == 1

    # Pod B — same dedup key, same day.
    caplog.set_level(logging.INFO)
    second = w._do_send_template(**common_kwargs)
    assert second == "", "second call must short-circuit (empty wamid)"
    assert route.call_count == 1, (
        "Meta API must NOT be called a second time on dedup short-circuit"
    )

    # Metric line was emitted.
    assert any(
        "metric=whatsapp_dedup_short_circuit_total" in r.getMessage()
        for r in caplog.records
    ), "expected whatsapp_dedup_short_circuit_total metric log line"


@respx.mock
def test_dedup_off_default_preserves_legacy_behavior(sqlite_session_factory):
    """With ``dedup_per_day`` defaulted to False, two calls both hit Meta.

    This guards against an accidental change that silently enabled dedup
    for callers that don't opt in (e.g. event-driven transactional sends).
    """
    Session = sqlite_session_factory
    user = _seed_user(Session)

    route = respx.post(
        "https://graph.facebook.com/v20.0/111/messages"
    ).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.x"}]})
    )

    common_kwargs = dict(
        to="+919876543210",
        template_name="nowlez_tomorrow_hearings_v1",  # in the allowlist
        language="en_US",
        variables={"count": "1", "formatted_list": "X"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=str(user.id),
        # dedup_per_day omitted -> False
    )

    w._do_send_template(**common_kwargs)
    w._do_send_template(**common_kwargs)

    assert route.call_count == 2, (
        "default dedup_per_day=False must NOT short-circuit; both calls "
        "should reach Meta"
    )


@respx.mock
def test_dedup_kwarg_on_transactional_template_is_a_noop(sqlite_session_factory):
    """``dedup_per_day=True`` on a template NOT in the allowlist still sends.

    Defence-in-depth: a producer that accidentally passes ``dedup_per_day=True``
    for a transactional template (signup welcome, order uploaded) must NOT
    silently drop the second send — the second send should reach Meta the
    same as it does today.
    """
    Session = sqlite_session_factory
    user = _seed_user(Session)

    route = respx.post(
        "https://graph.facebook.com/v20.0/111/messages"
    ).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.y"}]})
    )

    common_kwargs = dict(
        to="+919876543210",
        template_name="nowlez_signup_welcome_v1",  # NOT in allowlist
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=str(user.id),
        dedup_per_day=True,  # honored only for allowlisted templates
    )

    w._do_send_template(**common_kwargs)
    w._do_send_template(**common_kwargs)

    assert route.call_count == 2, (
        "transactional templates must bypass dedup even if the caller "
        "accidentally passes dedup_per_day=True"
    )


@respx.mock
def test_dedup_uses_ist_date_not_utc(monkeypatch, sqlite_session_factory):
    """The dedup key uses the IST date, not the UTC date.

    Schedules around UTC midnight (which is 05:30 IST) must not split a
    user's daily slot — at 19:00 UTC on May 19 (00:30 IST May 20), both
    pods must agree the slot belongs to May 20 IST.
    """
    Session = sqlite_session_factory
    user = _seed_user(Session)

    # Freeze time to 19:00 UTC = 00:30 IST next day.
    frozen_utc = datetime(2026, 5, 19, 19, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(w, "_utcnow", lambda: frozen_utc)

    # IST date for 19:00 UTC on May 19 is May 20 IST.
    expected_ist = date(2026, 5, 20)
    assert w._today_ist() == expected_ist

    respx.post(
        "https://graph.facebook.com/v20.0/111/messages"
    ).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.ist"}]})
    )

    w._do_send_template(
        to="+919876543210",
        template_name="nowlez_tomorrow_hearings_v1",
        language="en_US",
        variables={"count": "1", "formatted_list": "X"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id=str(user.id),
        dedup_per_day=True,
    )

    from data_access.models import WhatsAppDeliveryLog
    with Session() as s:
        row = s.query(WhatsAppDeliveryLog).filter_by(
            user_id=user.id,
        ).one()
        assert row.send_date_ist == expected_ist, (
            f"send_date_ist should be IST date {expected_ist}; got {row.send_date_ist}"
        )


def test_claim_helper_returns_true_when_no_user_id():
    """A worker invocation without ``user_id`` (ad-hoc / test path) is
    treated as 'claim successful' so unrelated callers don't change
    behavior."""
    ok = w._claim_daily_send_slot(
        user_id="",
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        rq_job_id=None,
    )
    assert ok is True


def test_dedup_daily_templates_includes_documented_set():
    """The allowlist must include the audit-cited templates.

    Removing one of these would silently re-introduce B-3. The set is
    intentionally narrow — adding a template should be a deliberate,
    reviewed change paired with a producer-side audit.
    """
    assert "nowlez_tomorrow_hearings_v1" in w._DEDUP_DAILY_TEMPLATES
    assert "nowlez_weekly_summary_v1" in w._DEDUP_DAILY_TEMPLATES
    # Transactional templates must NOT be in the allowlist.
    assert "nowlez_signup_welcome_v1" not in w._DEDUP_DAILY_TEMPLATES


# ---------------------------------------------------------------------------
# Tests for the components-flavored entry point (parity coverage)
# ---------------------------------------------------------------------------


@respx.mock
def test_template_with_components_dedup_short_circuits(
    sqlite_session_factory, caplog,
):
    """_do_send_template_with_components must honor dedup_per_day too."""
    Session = sqlite_session_factory
    user = _seed_user(Session)

    route = respx.post(
        "https://graph.facebook.com/v20.0/111/messages"
    ).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.cmp"}]})
    )

    common_kwargs = dict(
        to="+919876543210",
        template_name="nowlez_tomorrow_hearings_v1",
        language="en",
        body_variables=["1", "X"],
        brand="nowlez",
        header_media_id=None,
        button_url_variables=None,
        user_id=str(user.id),
        dedup_per_day=True,
    )

    w._do_send_template_with_components(**common_kwargs)
    caplog.set_level(logging.INFO)
    second = w._do_send_template_with_components(**common_kwargs)
    assert second == ""
    assert route.call_count == 1
    assert any(
        "metric=whatsapp_dedup_short_circuit_total" in r.getMessage()
        for r in caplog.records
    )
