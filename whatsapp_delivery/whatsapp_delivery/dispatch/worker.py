"""Worker functions executed by Munshi's RQ worker.

Each function is a top-level callable RQ can import. They construct fresh
:class:`MetaClient` / :class:`TemplateClient` instances per call (cheap; both
just wrap env vars + an httpx call).

Error handling per spec §6:
- :class:`MetaTransientError` → re-raise so RQ's ``Retry`` policy kicks in.
- :class:`Meta24HourWindowExpired` → swallowed locally and re-raised after a
  Sentry alert + a dead-letter log so the producer doesn't get re-retried;
  the only fix is filing a template or waiting for the user to message us.
- All other errors propagate naturally.

The :func:`process_send_queue` entry point is for ad-hoc local dev only —
production runs the worker process owned by Munshi (see
``0705/bot_scaffold/worker.py``).
"""
from __future__ import annotations

import logging
from typing import Any

from whatsapp_delivery.config import WhatsAppConfig
from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaTransientError,
)
from whatsapp_delivery.meta_client import MetaClient
from whatsapp_delivery.template_client import TemplateClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alert_dead_letter(reason: str, **ctx: Any) -> None:
    """Report a non-retryable failure to Sentry (if installed) and log it.

    Kept thin: Sentry is optional at runtime, so we wrap the import in
    try/except and degrade gracefully to a structured log line. Tests do not
    need to mock Sentry — the bare logger covers the assertion surface.
    """
    log.error("whatsapp_delivery dead-letter: %s ctx=%r", reason, ctx)
    try:
        import sentry_sdk  # noqa: PLC0415 — optional dep, import guarded

        # ``new_scope`` is the 2.x replacement for ``push_scope``; both still
        # land the extras on the captured event but new_scope is exception-
        # safe and re-entrant. Old versions of the SDK won't have it, so we
        # degrade to push_scope under a second guard.
        scope_cm = getattr(sentry_sdk, "new_scope", None) or sentry_sdk.push_scope
        with scope_cm() as scope:
            for k, v in ctx.items():
                scope.set_extra(k, v)
            sentry_sdk.capture_message(
                f"whatsapp_delivery dead-letter: {reason}",
                level="error",
            )
    except Exception:  # pragma: no cover — Sentry optional and best-effort
        pass


def _bind_wamid_to_delivery_log(rq_job_id: str | None, wamid: str) -> None:
    """Best-effort: link Meta's wamid back to the enqueue-side delivery row.

    Failures here are swallowed (and Sentry'd) because the send already
    succeeded — Munshi-side analytics can rebuild the link from Meta's
    status webhook if this misses.
    """
    if not (rq_job_id and wamid):
        return
    try:
        from data_access.daos.whatsapp_dao import set_meta_message_id
        from data_access.engine import get_session

        with get_session() as s:
            set_meta_message_id(s, rq_job_id=rq_job_id, meta_message_id=wamid)
    except Exception as e:  # pragma: no cover — best-effort linkage
        log.warning("failed to bind wamid=%s to job=%s: %s", wamid, rq_job_id, e)


# ---------------------------------------------------------------------------
# Job entry points
# ---------------------------------------------------------------------------


def _do_send_text(
    *,
    to: str,
    body: str,
    brand: str,
    user_id: str | None = None,
) -> str:
    """RQ entry: send a free-text message."""
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        log.warning("nowlez kill-switch on; skipping send_text to=%s", to)
        return ""
    client = MetaClient(
        phone_number_id=cfg.meta_phone_number_id,
        access_token=cfg.meta_access_token,
    )
    try:
        return client.send_text(to, body)
    except MetaTransientError:
        # Allow RQ's retry to handle this — the producer enqueued with Retry().
        raise
    except Meta24HourWindowExpired as e:
        _alert_dead_letter(
            "24h_window_expired_send_text",
            to=to,
            brand=brand,
            user_id=user_id,
            err=str(e),
        )
        raise


def _do_send_template(
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
) -> str:
    """RQ entry: send a registry-backed template by name + variable dict.

    Looks the template up in the registry, picks the variable ordering Meta
    expects from the template spec, optionally uploads a document header,
    and dispatches via :class:`TemplateClient`.
    """
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        log.warning(
            "nowlez kill-switch on; skipping send_template name=%s to=%s",
            template_name,
            to,
        )
        return ""

    # Lazy import so the templates package can be torn down/replaced in tests
    # without forcing every queue.py import to also pull YAML loaders.
    from whatsapp_delivery.templates import get_template

    template = get_template(template_name, language)

    media_id: str | None = None
    if template.has_document_header() and media_bytes:
        media_client = MetaClient(
            phone_number_id=cfg.meta_phone_number_id,
            access_token=cfg.meta_access_token,
        )
        media_id = media_client.upload_media(
            data=media_bytes,
            filename=media_filename or "document.pdf",
            mime_type=media_mime,
        )

    body_vars = [variables[v.name] for v in template.body_variables_in_order()]
    button_vars = [
        variables[v.name] for v in template.button_url_variables_in_order()
    ]

    tmpl_client = TemplateClient(
        phone_number_id=cfg.meta_phone_number_id,
        access_token=cfg.meta_access_token,
    )
    try:
        wamid = tmpl_client.send_template_with_components(
            to=to,
            name=template.full_name,
            language=language,
            body_variables=body_vars,
            header_media_id=media_id,
            button_url_variables=button_vars or None,
        )
    except MetaTransientError:
        raise
    except Meta24HourWindowExpired as e:
        # Templates SHOULD bypass the 24h window — if we see this, the
        # template name is wrong or Meta hasn't approved it yet.
        _alert_dead_letter(
            "24h_window_expired_send_template",
            to=to,
            template_name=template_name,
            language=language,
            brand=brand,
            user_id=user_id,
            err=str(e),
        )
        raise

    _bind_wamid_to_delivery_log(_current_rq_job_id(), wamid)
    return wamid


def _do_send_template_with_components(
    *,
    to: str,
    template_name: str,
    language: str,
    body_variables: list[str],
    brand: str,
    header_media_id: str | None = None,
    button_url_variables: list[str] | None = None,
    user_id: str | None = None,
) -> str:
    """RQ entry: low-level template send (positional vars, pre-uploaded media)."""
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        log.warning(
            "nowlez kill-switch on; skipping send_template_with_components name=%s to=%s",
            template_name,
            to,
        )
        return ""

    client = TemplateClient(
        phone_number_id=cfg.meta_phone_number_id,
        access_token=cfg.meta_access_token,
    )
    try:
        wamid = client.send_template_with_components(
            to=to,
            name=template_name,
            language=language,
            body_variables=body_variables,
            header_media_id=header_media_id,
            button_url_variables=button_url_variables,
        )
    except MetaTransientError:
        raise
    except Meta24HourWindowExpired as e:
        _alert_dead_letter(
            "24h_window_expired_send_template",
            to=to,
            template_name=template_name,
            language=language,
            brand=brand,
            user_id=user_id,
            err=str(e),
        )
        raise

    _bind_wamid_to_delivery_log(_current_rq_job_id(), wamid)
    return wamid


def _do_send_document(
    *,
    to: str,
    document_bytes: bytes,
    caption: str,
    filename: str,
    brand: str,
) -> str:
    """RQ entry: upload + send a PDF in one call."""
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        return ""
    client = MetaClient(
        phone_number_id=cfg.meta_phone_number_id,
        access_token=cfg.meta_access_token,
    )
    try:
        return client.send_document_from_bytes(
            to,
            data=document_bytes,
            filename=filename,
            caption=caption,
        )
    except MetaTransientError:
        raise
    except Meta24HourWindowExpired as e:
        _alert_dead_letter(
            "24h_window_expired_send_document",
            to=to,
            brand=brand,
            err=str(e),
        )
        raise


def _current_rq_job_id() -> str | None:
    """Return the job id of the currently-executing RQ job, or None.

    Called from inside a job; uses ``rq.get_current_job()`` so we don't need
    callers to thread the job-id through every kwarg.
    """
    try:
        from rq import get_current_job

        job = get_current_job()
        return job.id if job else None
    except Exception:  # pragma: no cover — only triggered outside a worker
        return None


# ---------------------------------------------------------------------------
# Standalone entry point (ad-hoc local dev only)
# ---------------------------------------------------------------------------


def process_send_queue() -> None:
    """Entry point for ``python -m whatsapp_delivery.dispatch.worker``.

    Production drains ``whatsapp_send`` via Munshi's existing worker (see
    ``0705/bot_scaffold/worker.py`` — its queues list was extended in
    Task 6.3 to include ``whatsapp_send``). This function exists for local
    dev where you want a one-off worker without Munshi running.
    """
    from redis import Redis
    from rq import Worker

    cfg = WhatsAppConfig()
    conn = Redis.from_url(cfg.shared_redis_url)
    Worker(["whatsapp_send"], connection=conn).work()


if __name__ == "__main__":  # pragma: no cover
    process_send_queue()
