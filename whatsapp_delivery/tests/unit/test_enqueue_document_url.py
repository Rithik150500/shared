"""Audit fix D-5: ``enqueue_send_document`` takes a URL, not raw bytes.

Background: the original audit flagged the bytes-based API for pickling
raw PDF bytes into Redis as RQ job kwargs — a 5 MB PDF × 3 retries ×
failed-job-registry retention = ~60 MB Redis bloat per failed job. Under
a sustained outage that pushes Redis OOM.

An earlier mitigation (commit 4f2feb9) capped ``document_bytes`` at
256 KB to bound the immediate risk. This file supersedes that cap.
``enqueue_send_document`` now takes a ``document_url`` string — the
queue carries a URL (tens of bytes) instead of a PDF (MB). The worker
fetches bytes just-in-time before the Meta upload, so the Redis-side
payload is always small regardless of document size.

Two URL schemes are accepted:

- ``file:///abs/path`` — worker reads from the local filesystem. Used
  when producer + worker run on the same host (case-tracker today).
- ``http(s)://...`` — worker does an HTTP GET. Used when the producer
  has already staged the file to object storage (R2/S3/Cubbit). The
  worker caps the response at 16 MB to defend against a malicious URL
  returning gigabytes.

There are still NO production callers of ``enqueue_send_document`` —
the audit note in the now-deleted ``test_enqueue_document_size_cap.py``
held when the URL refactor landed, so the signature change is
zero-risk. See git blame on this file for the rationale.
"""
from __future__ import annotations

import asyncio

import pytest

from whatsapp_delivery.dispatch.queue import enqueue_send_document


def test_enqueue_send_document_puts_url_in_job_kwargs(monkeypatch):
    """The URL string lands in job kwargs verbatim — no bytes anywhere."""
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

    job_id = asyncio.run(enqueue_send_document(
        to="+919999999999",
        document_url="file:///tmp/test.pdf",
        caption="test",
        filename="test.pdf",
        brand="munshi",
    ))
    assert job_id == "job-fake"
    job_kwargs = enqueued["kwargs"]["kwargs"]
    # The URL is in the job kwargs verbatim. Bytes are NEVER pickled.
    assert job_kwargs["document_url"] == "file:///tmp/test.pdf"
    assert "document_bytes" not in job_kwargs
    assert job_kwargs["filename"] == "test.pdf"
    assert job_kwargs["caption"] == "test"
    assert job_kwargs["brand"] == "munshi"


def test_enqueue_send_document_accepts_https_url(monkeypatch):
    """HTTPS URLs are accepted unchanged (worker resolves at send time)."""
    from whatsapp_delivery.dispatch import queue as q

    captured: dict = {}

    class _FakeJob:
        id = "job-https"

    class _FakeQueue:
        def enqueue(self, func, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeJob()

    monkeypatch.setattr(q, "_get_queue", lambda: _FakeQueue())

    asyncio.run(enqueue_send_document(
        to="+919999999999",
        document_url="https://r2.example.com/orders/abc.pdf",
        caption="Order PDF",
        filename="order.pdf",
        brand="munshi",
    ))
    assert (
        captured["kwargs"]["kwargs"]["document_url"]
        == "https://r2.example.com/orders/abc.pdf"
    )


def test_enqueue_send_document_no_byte_cap_remaining(monkeypatch):
    """The 256 KB ``_MAX_DOCUMENT_BYTES`` cap should NOT exist anymore.

    With bytes out of the queue path, the cap has nothing to protect; the
    worker enforces its own size cap when it fetches from the URL.
    """
    from whatsapp_delivery.dispatch import queue as q

    assert not hasattr(q, "_MAX_DOCUMENT_BYTES"), (
        "queue._MAX_DOCUMENT_BYTES should have been removed in the URL refactor "
        "— bytes never enter Redis now, so the cap is obsolete."
    )


def test_enqueue_send_document_rejects_bytes_kwarg():
    """A caller that still passes ``document_bytes`` gets a TypeError.

    This is a deliberate breaking change of the API: at refactor time there
    were zero production callers (see module docstring) so renaming is safe.
    Anyone updating to the new signature should swap to ``document_url``.
    """
    with pytest.raises(TypeError):
        asyncio.run(enqueue_send_document(
            to="+919999999999",
            document_bytes=b"%PDF-1.4\n...",  # type: ignore[call-arg]
            caption="test",
            filename="test.pdf",
            brand="munshi",
        ))
