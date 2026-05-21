"""Async enqueue helpers for WhatsApp sends.

All public API functions are ``async def`` to match the (mostly async) callers,
but the underlying RQ ``enqueue`` is sync. We wrap with ``asyncio.to_thread``.

The Redis connection is constructed lazily per enqueue (cheap; just a URL
parse and a TCP connect that gets pooled by ``redis-py``) so tests can
monkey-patch ``_get_redis`` without having to reach into module state.
"""
from __future__ import annotations

import asyncio
from typing import Any

from redis import Redis
from rq import Queue
from rq.job import Retry

from whatsapp_delivery.config import WhatsAppConfig

_QUEUE_NAME = "whatsapp_send"
# 1m → 5m → 15m. RQ's Retry takes either a single int (constant back-off) or
# an int-per-attempt list; we use the list form so the third attempt waits a
# full quarter-hour, which is the soak time Meta documents for transient
# 5xx-class incidents.
_RETRY_POLICY = Retry(max=3, interval=[60, 300, 900])


def _get_redis() -> Redis:
    """Construct a fresh Redis connection from the shared config.

    Replaced by tests via ``monkeypatch.setattr(queue, '_get_redis', ...)`` so
    fakeredis can stand in without touching env vars.
    """
    cfg = WhatsAppConfig()
    return Redis.from_url(cfg.shared_redis_url)


def _get_queue() -> Queue:
    return Queue(_QUEUE_NAME, connection=_get_redis())


async def enqueue_send_text(
    *,
    to: str,
    body: str,
    brand: str,
    user_id: str | None = None,
    dedup_key: str | None = None,
) -> str:
    """Enqueue a free-text send. Returns the RQ job id.

    Args:
      dedup_key: D-4 producer-side idempotency. When provided, the worker
        claims a Redis SETNX slot keyed by ``send_dedup:<dedup_key>``
        (10-min TTL) before sending; a second job execution for the same
        key (e.g. RQ retry of an already-acknowledged enqueue) short-
        circuits without calling Meta. Default ``None`` preserves the
        existing fire-and-forget behavior.
    """

    def _enqueue() -> str:
        # Import inside the closure so RQ's pickler captures the worker
        # function by qualified name, not by a free variable that would force
        # the producer process to keep the worker module imported.
        from whatsapp_delivery.dispatch.worker import _do_send_text

        job = _get_queue().enqueue(
            _do_send_text,
            kwargs={
                "to": to,
                "body": body,
                "brand": brand,
                "user_id": user_id,
                "dedup_key": dedup_key,
            },
            retry=_RETRY_POLICY,
            job_timeout="2m",
        )
        return job.id

    return await asyncio.to_thread(_enqueue)


async def enqueue_send_template(
    *,
    to: str,
    template_name: str,
    language: str,
    variables: dict[str, Any],
    brand: str,
    media_bytes: bytes | None = None,
    media_filename: str | None = None,
    media_mime: str = "application/pdf",
    related_case_id: str | None = None,
    related_order_id: str | None = None,
    user_id: str | None = None,
    dedup_per_day: bool = False,
    dedup_key: str | None = None,
) -> str:
    """Enqueue a template send (body + optional document header + URL button).

    Returns the RQ job id; the wamid is bound to the producer-side
    ``whatsapp_delivery_log`` row by the worker once Meta acknowledges the
    send.

    Args:
      dedup_per_day: B-3 cross-pod dedup. When True the worker
        short-circuits if another pod has already sent this
        ``(user_id, template_name)`` pair today (IST). Default False
        (transactional sends preserve fire-and-forget). Only callers
        running on a once-per-user-per-day cron should pass True — see
        ``shared/whatsapp_delivery/dispatch/worker.py:_DEDUP_DAILY_TEMPLATES``
        for the authoritative allowlist.
      dedup_key: D-4 producer-side idempotency. When provided, the worker
        claims a Redis SETNX slot keyed by ``send_dedup:<dedup_key>``
        (10-min TTL) and short-circuits any subsequent execution with the
        same key. Use this for transactional sends (signup welcome, OTP,
        order-uploaded) so an RQ retry of an already-acknowledged job
        does not double-bill Meta or double-message the user. Independent
        of ``dedup_per_day`` (they use different storage and can both be
        in effect).
    """

    def _enqueue() -> str:
        from whatsapp_delivery.dispatch.worker import _do_send_template

        job = _get_queue().enqueue(
            _do_send_template,
            kwargs={
                "to": to,
                "template_name": template_name,
                "language": language,
                "variables": variables,
                "brand": brand,
                "media_bytes": media_bytes,
                "media_filename": media_filename,
                "media_mime": media_mime,
                "related_case_id": related_case_id,
                "related_order_id": related_order_id,
                "user_id": user_id,
                "dedup_per_day": dedup_per_day,
                "dedup_key": dedup_key,
            },
            retry=_RETRY_POLICY,
            job_timeout="2m",
        )
        return job.id

    return await asyncio.to_thread(_enqueue)


async def enqueue_send_template_with_components(
    *,
    to: str,
    template_name: str,
    language: str,
    body_variables: list[str],
    brand: str,
    header_media_id: str | None = None,
    button_url_variables: list[str] | None = None,
    user_id: str | None = None,
    dedup_per_day: bool = False,
    dedup_key: str | None = None,
) -> str:
    """Enqueue a low-level template send when the caller already has positional
    variables + a pre-uploaded media id.

    Used by Munshi's existing notifier code (which already builds the
    component args itself) — callers that just have a ``dict`` of variable
    names should use :func:`enqueue_send_template` instead.

    ``dedup_per_day`` and ``dedup_key`` have the same semantics as in
    :func:`enqueue_send_template`.
    """

    def _enqueue() -> str:
        from whatsapp_delivery.dispatch.worker import _do_send_template_with_components

        job = _get_queue().enqueue(
            _do_send_template_with_components,
            kwargs={
                "to": to,
                "template_name": template_name,
                "language": language,
                "body_variables": body_variables,
                "brand": brand,
                "header_media_id": header_media_id,
                "button_url_variables": button_url_variables,
                "user_id": user_id,
                "dedup_per_day": dedup_per_day,
                "dedup_key": dedup_key,
            },
            retry=_RETRY_POLICY,
            job_timeout="2m",
        )
        return job.id

    return await asyncio.to_thread(_enqueue)


async def enqueue_send_document(
    *,
    to: str,
    document_url: str,
    caption: str,
    filename: str,
    brand: str,
    dedup_key: str | None = None,
) -> str:
    """Enqueue an upload-and-send PDF job. Returns the RQ job id.

    Audit fix D-5: ``document_url`` is a *string*, not raw bytes. The
    worker fetches the bytes just-in-time before the Meta upload, so
    even a 5 MB PDF only costs ~tens of bytes of Redis space per
    queued job — eliminating the failed-job-registry OOM risk that
    the earlier ``document_bytes`` API created.

    Two URL schemes are accepted by the worker:

    - ``file:///abs/path`` — worker reads the file from the local
      filesystem. Use when producer and worker run on the same host
      (case-tracker today). Cheap; no network round-trip.
    - ``http(s)://...`` — worker does an HTTP GET. Use when the producer
      has already staged the file to object storage (R2/S3/Cubbit).
      The worker enforces a 16 MB size cap on the response to defend
      against malicious URLs returning gigabytes (see
      ``_DOCUMENT_MAX_BYTES`` in ``dispatch.worker``).

    ``dedup_key`` has the same D-4 semantics as in
    :func:`enqueue_send_text`: a stable producer-supplied key that
    makes RQ retries idempotent (and avoids re-fetching + re-uploading
    the same PDF).
    """

    def _enqueue() -> str:
        from whatsapp_delivery.dispatch.worker import _do_send_document

        job = _get_queue().enqueue(
            _do_send_document,
            kwargs={
                "to": to,
                "document_url": document_url,
                "caption": caption,
                "filename": filename,
                "brand": brand,
                "dedup_key": dedup_key,
            },
            retry=_RETRY_POLICY,
            job_timeout="2m",
        )
        return job.id

    return await asyncio.to_thread(_enqueue)
