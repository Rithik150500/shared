"""Phase 1: upsell DAO integration.

Covers the SQL surface of case_billing.nowlez.upsell using in-memory
SQLite + Base.metadata.create_all():
- was_stage_sent returns False / True at the right moments.
- record_upsell_event round-trips.
- UNIQUE(user_id, stage) enforces idempotency.
- record_upgrade_conversion marks the most-recent unconverted event.
- record_upgrade_conversion is a no-op when no events exist.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from case_billing.nowlez.upsell import (
    record_upgrade_conversion,
    record_upsell_event,
    was_stage_sent,
)
from data_access.base import Base
from data_access.models import MunshiUpsellEvent, User


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def user(session):
    u = User(phone="+919876543210", locale="en")
    session.add(u)
    session.commit()
    return u


def test_was_stage_sent_returns_false_for_new_user(session, user):
    assert was_stage_sent(session, user_id=user.id, stage="initial") is False


def test_record_upsell_event_round_trip(session, user):
    record_upsell_event(
        session, user_id=user.id, stage="initial",
        trigger_reason="case_count",
        case_count_at_send=80, spend_at_send_rupees=0,
        template_name="munshi_upsell_initial_v1",
    )
    session.commit()
    assert was_stage_sent(session, user_id=user.id, stage="initial") is True
    rows = session.execute(select(MunshiUpsellEvent)).scalars().all()
    assert len(rows) == 1
    assert rows[0].stage == "initial"
    assert rows[0].trigger_reason == "case_count"
    assert rows[0].case_count_at_send == 80
    assert rows[0].template_name == "munshi_upsell_initial_v1"


def test_unique_constraint_blocks_duplicate_stage(session, user):
    """The DB UNIQUE(user_id, stage) constraint enforces idempotency."""
    record_upsell_event(
        session, user_id=user.id, stage="initial",
        trigger_reason="case_count",
        case_count_at_send=80, spend_at_send_rupees=0,
        template_name="munshi_upsell_initial_v1",
    )
    session.commit()
    # Second insert of same stage should raise IntegrityError on commit.
    record_upsell_event(
        session, user_id=user.id, stage="initial",
        trigger_reason="case_count",
        case_count_at_send=120, spend_at_send_rupees=0,
        template_name="munshi_upsell_initial_v1",
    )
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_record_upgrade_conversion_marks_most_recent_event(session, user):
    record_upsell_event(
        session, user_id=user.id, stage="initial",
        trigger_reason="case_count",
        case_count_at_send=80, spend_at_send_rupees=0,
        template_name="munshi_upsell_initial_v1",
    )
    record_upsell_event(
        session, user_id=user.id, stage="reminder",
        trigger_reason="case_count",
        case_count_at_send=150, spend_at_send_rupees=0,
        template_name="munshi_upsell_reminder_v1",
    )
    session.commit()

    record_upgrade_conversion(session, user_id=user.id, tier="chambers")
    session.commit()

    rows = session.execute(
        select(MunshiUpsellEvent).order_by(MunshiUpsellEvent.sent_at)
    ).scalars().all()
    # The MORE RECENT event (reminder) should be marked converted.
    assert rows[0].stage == "initial"
    assert rows[0].converted_at is None  # initial NOT marked
    assert rows[1].stage == "reminder"
    assert rows[1].converted_at is not None
    assert rows[1].converted_to_tier == "chambers"


def test_record_upgrade_conversion_no_op_when_no_unconverted_events(session, user):
    # No upsell events at all — function must not raise.
    record_upgrade_conversion(session, user_id=user.id, tier="chambers")
    session.commit()
    # No events should exist.
    assert session.execute(select(MunshiUpsellEvent)).scalars().all() == []


def test_record_upgrade_conversion_skips_already_converted_events(session, user):
    """If only converted events exist, the function is a no-op."""
    record_upsell_event(
        session, user_id=user.id, stage="initial",
        trigger_reason="case_count",
        case_count_at_send=80, spend_at_send_rupees=0,
        template_name="munshi_upsell_initial_v1",
    )
    session.commit()
    # First conversion marks the event.
    record_upgrade_conversion(session, user_id=user.id, tier="chambers")
    session.commit()

    # Second conversion (e.g., webhook redelivery on a downgrade->reupgrade)
    # should NOT re-mark the already-converted event because the filter
    # is converted_at IS NULL.
    rows = session.execute(select(MunshiUpsellEvent)).scalars().all()
    [first_row] = rows
    first_converted_at = first_row.converted_at
    record_upgrade_conversion(session, user_id=user.id, tier="counsel")
    session.commit()
    rows = session.execute(select(MunshiUpsellEvent)).scalars().all()
    [refreshed] = rows
    # Still the original conversion timestamp/tier.
    assert refreshed.converted_at == first_converted_at
    assert refreshed.converted_to_tier == "chambers"
