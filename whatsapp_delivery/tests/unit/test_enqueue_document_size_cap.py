"""Audit fix D-5: enqueue_send_document caps document_bytes at 256 KB.

Background: the original audit flagged ``enqueue_send_document`` for
pickling raw PDF bytes into Redis as RQ job kwargs — a 5 MB PDF × 3
retries × failed-job-registry retention = ~60 MB Redis bloat per failed
job. Under a sustained outage that pushes Redis OOM.

There are currently NO production callers of ``enqueue_send_document``
(grep across 0705 + casepilot returns zero hits), so the practical risk
is theoretical. Rather than build S3 staging infrastructure for an
unused feature, the fix is to cap document_bytes at 256 KB so any future
caller gets an immediate clear error if they try to send a large
document — at which point a real staging helper can be designed against
a concrete use case.
"""
from __future__ import annotations

import asyncio

import pytest

from whatsapp_delivery.dispatch.queue import (
    _MAX_DOCUMENT_BYTES,
    enqueue_send_document,
)


def test_max_document_bytes_constant_is_256kb():
    """Spec pin: cap stays at 256 KB unless deliberately changed."""
    assert _MAX_DOCUMENT_BYTES == 256 * 1024


def test_enqueue_send_document_raises_for_oversized_payload():
    """Sending a 256 KB + 1 byte payload should raise ValueError."""
    oversized = b"x" * (_MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds _MAX_DOCUMENT_BYTES"):
        asyncio.run(enqueue_send_document(
            to="+919999999999",
            document_bytes=oversized,
            caption="test",
            filename="big.pdf",
            brand="munshi",
        ))


def test_enqueue_send_document_error_includes_actual_size():
    """The ValueError message should report the offending size for debugging."""
    oversized = b"x" * (_MAX_DOCUMENT_BYTES + 100)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(enqueue_send_document(
            to="+919999999999",
            document_bytes=oversized,
            caption="test",
            filename="big.pdf",
            brand="munshi",
        ))
    msg = str(excinfo.value)
    # The message format is "X,XXX bytes which exceeds _MAX_DOCUMENT_BYTES (Y,YYY)"
    assert str(_MAX_DOCUMENT_BYTES + 100) in msg.replace(",", "")
    assert "256" in msg  # the cap is mentioned


def test_enqueue_send_document_accepts_at_cap_boundary(monkeypatch):
    """Exactly at the cap (256 KB) should NOT raise. Mock Redis to avoid
    real enqueue."""
    from whatsapp_delivery.dispatch import queue as q

    enqueued: dict = {}

    class _FakeJob:
        id = "job-fake"

    class _FakeQueue:
        def enqueue(self, func, **kwargs):
            enqueued["called"] = True
            enqueued["kwargs"] = kwargs
            return _FakeJob()

    monkeypatch.setattr(q, "_get_queue", lambda: _FakeQueue())

    at_cap = b"x" * _MAX_DOCUMENT_BYTES
    job_id = asyncio.run(enqueue_send_document(
        to="+919999999999",
        document_bytes=at_cap,
        caption="test",
        filename="ok.pdf",
        brand="munshi",
    ))
    assert job_id == "job-fake"
    assert enqueued.get("called") is True
    assert enqueued["kwargs"]["kwargs"]["document_bytes"] == at_cap
