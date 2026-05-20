"""Tests for ``whatsapp_delivery.dispatch.queue``.

Uses ``fakeredis`` for the queue + monkey-patches ``_get_redis`` so no real
Redis is needed. Verifies the job that lands on the queue has the right
function reference and kwargs — the actual send-side behaviour is covered
by ``test_worker.py``.
"""
from __future__ import annotations

import asyncio

import fakeredis
import pytest
from rq import Queue

from whatsapp_delivery.dispatch import queue as q


@pytest.fixture
def fake_redis(monkeypatch):
    fr = fakeredis.FakeRedis()
    monkeypatch.setattr(q, "_get_redis", lambda: fr)
    return fr


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_enqueue_send_text_returns_job_id(fake_redis):
    job_id = _run(
        q.enqueue_send_text(
            to="+919999999999",
            body="hello",
            brand="munshi",
            user_id="u-1",
        )
    )
    assert job_id

    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    job = rq_queue.jobs[0]
    # RQ resolves the func by qualified name; the trailing segment is the
    # callable name, which we lock to the private leading-underscore handle.
    assert job.func_name.endswith("_do_send_text")
    kwargs = job.kwargs
    assert kwargs == {
        "to": "+919999999999",
        "body": "hello",
        "brand": "munshi",
        "user_id": "u-1",
    }


def test_enqueue_send_template_returns_job_id(fake_redis):
    job_id = _run(
        q.enqueue_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v1",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
        )
    )
    assert job_id

    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    job = rq_queue.jobs[0]
    assert job.func_name.endswith("_do_send_template")
    kwargs = job.kwargs
    assert kwargs["to"] == "+919999999999"
    assert kwargs["template_name"] == "nowlez_signup_welcome_v1"
    assert kwargs["language"] == "en_US"
    assert kwargs["variables"] == {"user_name": "Asha"}
    assert kwargs["brand"] == "nowlez"


def test_enqueue_send_template_with_components(fake_redis):
    job_id = _run(
        q.enqueue_send_template_with_components(
            to="+919999999999",
            template_name="munshi_new_order_v1",
            language="en",
            body_variables=["Mehta v. State", "01 Jan 2026", "ORD-1"],
            brand="munshi",
            header_media_id="media-123",
            button_url_variables=None,
        )
    )
    assert job_id

    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    job = rq_queue.jobs[0]
    assert job.func_name.endswith("_do_send_template_with_components")
    kwargs = job.kwargs
    assert kwargs["body_variables"] == [
        "Mehta v. State",
        "01 Jan 2026",
        "ORD-1",
    ]
    assert kwargs["header_media_id"] == "media-123"
    assert kwargs["button_url_variables"] is None


def test_enqueue_send_document(fake_redis):
    job_id = _run(
        q.enqueue_send_document(
            to="+919999999999",
            document_bytes=b"%PDF-1.4\n...",
            caption="Order PDF",
            filename="order.pdf",
            brand="munshi",
        )
    )
    assert job_id

    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    job = rq_queue.jobs[0]
    assert job.func_name.endswith("_do_send_document")
    kwargs = job.kwargs
    assert kwargs["filename"] == "order.pdf"
    assert kwargs["caption"] == "Order PDF"
    assert kwargs["document_bytes"] == b"%PDF-1.4\n..."


def test_queue_uses_whatsapp_send_name(fake_redis):
    """Producer + worker must agree on the queue name."""
    _run(
        q.enqueue_send_text(
            to="+1", body="x", brand="munshi", user_id=None,
        )
    )
    # The queue we wrote into must be 'whatsapp_send', not RQ's default.
    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    default_queue = Queue("default", connection=fake_redis)
    assert default_queue.count == 0


def test_enqueue_attaches_retry_policy(fake_redis):
    """The producer sets a 3-try retry so transient Meta 5xx self-heals."""
    _run(
        q.enqueue_send_text(
            to="+1", body="x", brand="munshi", user_id=None,
        )
    )
    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    job = rq_queue.jobs[0]
    # RQ stores retry state on the job; the exact attribute name is
    # version-dependent but `retries_left` / `retry_intervals` are stable
    # across 1.x/2.x. Use defensive getattr so the test doesn't bind to
    # implementation detail.
    retries_left = getattr(job, "retries_left", None)
    assert retries_left == 3


def test_enqueue_send_template_threads_dedup_per_day(fake_redis):
    """B-3: producer's ``dedup_per_day`` kwarg lands on the worker job kwargs."""
    _run(
        q.enqueue_send_template(
            to="+919999999999",
            template_name="nowlez_tomorrow_hearings_v1",
            language="en_US",
            variables={"count": "1", "formatted_list": "X"},
            brand="nowlez",
            user_id="u-1",
            dedup_per_day=True,
        )
    )
    job = Queue("whatsapp_send", connection=fake_redis).jobs[0]
    assert job.kwargs["dedup_per_day"] is True


def test_enqueue_send_template_dedup_default_false(fake_redis):
    """B-3 safety: default is False so transactional callers are unaffected."""
    _run(
        q.enqueue_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v1",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
            user_id="u-2",
        )
    )
    job = Queue("whatsapp_send", connection=fake_redis).jobs[0]
    assert job.kwargs["dedup_per_day"] is False


def test_enqueue_send_template_with_components_threads_dedup_per_day(fake_redis):
    """The components-flavored producer also threads the kwarg through."""
    _run(
        q.enqueue_send_template_with_components(
            to="+919999999999",
            template_name="nowlez_tomorrow_hearings_v1",
            language="en",
            body_variables=["1", "X"],
            brand="nowlez",
            user_id="u-3",
            dedup_per_day=True,
        )
    )
    job = Queue("whatsapp_send", connection=fake_redis).jobs[0]
    assert job.kwargs["dedup_per_day"] is True
