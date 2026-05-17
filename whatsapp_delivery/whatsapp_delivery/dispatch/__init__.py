"""Async dispatch: enqueue sends to RQ; Munshi's worker drains.

Public API (per plan §6.1):
- ``enqueue_send_text``      — free-text body (used inside the 24h window).
- ``enqueue_send_template``  — single-body-component templates (legacy shape).
- ``enqueue_send_template_with_components`` — full template with optional
  document header + URL-button variables (the shape every ``nowlez_*``
  template uses).
- ``enqueue_send_document``  — upload-and-send a PDF in one job.

All callers are mostly async; the RQ ``enqueue`` itself is sync so each helper
wraps the sync work in ``asyncio.to_thread``. Jobs are pushed to the
``whatsapp_send`` queue on the shared Redis (see :class:`WhatsAppConfig`).
"""
from whatsapp_delivery.dispatch.queue import (
    enqueue_send_document,
    enqueue_send_template,
    enqueue_send_template_with_components,
    enqueue_send_text,
)

__all__ = [
    "enqueue_send_document",
    "enqueue_send_template",
    "enqueue_send_template_with_components",
    "enqueue_send_text",
]
