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

**B-3 cross-pod dedup (audit fix):** When the producer passes
``dedup_per_day=True``, the template worker first attempts an INSERT ...
ON CONFLICT DO NOTHING claim on ``whatsapp_delivery_log`` keyed by
``(user_id, template_name, send_date_ist)`` before calling Meta. If another
pod already owns the slot the worker short-circuits without sending.
Default is ``False`` so transactional sends (signup welcome, OTP, order
upload notifications) preserve their current fire-and-forget behavior —
those templates may legitimately fire multiple times per user per day.

**Crash safety note:** the claim row is written BEFORE the Meta call, so if
the worker crashes between claim and send the row is left as ``pending``
forever and that user misses the send for that day. For daily-cadence
templates this is acceptable: the next day's cron retries. For one-shot
transactional sends (signup welcome, OTP) the dedup is INTENTIONALLY off —
those use the existing producer-side ``log_send_enqueued`` and rely on the
caller's idempotency, not the per-day key.

The :func:`process_send_queue` entry point is for ad-hoc local dev only —
production runs the worker process owned by Munshi (see
``0705/bot_scaffold/worker.py``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from redis import Redis

from whatsapp_delivery.config import WhatsAppConfig
from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaTransientError,
)
from whatsapp_delivery.meta_client import MetaClient
from whatsapp_delivery.template_client import TemplateClient

log = logging.getLogger(__name__)


# D-4 producer-side idempotency. When the producer passes a stable
# ``dedup_key`` we treat the FIRST job execution as the authoritative send
# and short-circuit any subsequent execution (RQ retry, accidental
# re-enqueue) that lands inside the TTL window. The 10-minute TTL is chosen
# to comfortably cover the retry policy (1m -> 5m -> 15m back-off = 21m
# max) without holding the key forever — once the retry budget is
# exhausted the key naturally expires and a fresh enqueue is allowed.
#
# Distinct from B-3's per-day dedup: that one uses a DB-backed
# ``(user_id, template_name, send_date_ist)`` claim row for daily-cadence
# templates. This one uses a Redis SETNX keyed by an arbitrary string
# supplied by the producer, suitable for transactional sends (signup
# welcome, OTP, order-uploaded, stop confirmation) where the natural
# dedup key is e.g. ``f"signup:{user_id}"`` or ``f"order:{order_id}"``.
# The two systems intentionally use DIFFERENT key prefixes/storage so
# they can't collide.
_SEND_DEDUP_PREFIX = "send_dedup:"
_SEND_DEDUP_TTL_SECONDS = 600  # 10 minutes; safely covers the retry budget.


# B-3 dedup allowlist: templates whose cron fires at most once per
# user per IST day. The producer side (casepilot/backend/scheduler.py) is
# the source of truth; this allowlist exists so a misconfigured
# ``dedup_per_day=True`` on a transactional template (e.g. accidental
# refactor) doesn't silently silence sends. The set is intentionally small
# and explicit — extending it should be paired with a producer-side
# audit.
#
# Keep in sync with ``_DAILY_CADENCE_TEMPLATES`` in
# ``shared/data-access/data_access/alembic/versions/20260606_b3_dedup_send_per_day.py``.
_DEDUP_DAILY_TEMPLATES: frozenset[str] = frozenset({
    "nowlez_tomorrow_hearings_v1",
    "nowlez_weekly_summary_v1",
})

# IST timezone for per-day key computation. Computed once at module load —
# ``ZoneInfo`` is cheap to look up but doing it per-send is wasteful.
_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_redis() -> Redis:
    """Construct a Redis connection from the shared config.

    Mirrors ``queue._get_redis`` so tests can monkey-patch this single
    symbol to stand in fakeredis. Kept module-local rather than imported
    from queue.py to avoid an import cycle (queue.py imports from worker
    for the RQ callable references).
    """
    cfg = WhatsAppConfig()
    return Redis.from_url(cfg.shared_redis_url)


def _claim_send_dedup_key(dedup_key: str | None) -> bool:
    """Try to claim a producer-supplied dedup key. Return True on first claim.

    Uses Redis SETNX with the documented prefix + TTL. If the key is
    already set (a prior worker execution for the same logical send
    already completed) the function returns False and the worker SHOULD
    short-circuit without calling Meta.

    Best-effort: if Redis is unreachable we log and return True so the
    worker still attempts the send — the alternative (silently dropping
    sends on a Redis hiccup) is worse than an occasional duplicate.

    ``None``/empty dedup_key returns True (claim is a no-op) so callers
    that don't opt in keep their existing behavior.
    """
    if not dedup_key:
        return True
    try:
        client = _get_redis()
        # ``nx=True`` makes set fail (return None/False) if the key already
        # exists; ``ex`` sets the TTL atomically with the write so we never
        # leave a no-TTL key behind on a connection drop between SET and
        # EXPIRE.
        ok = client.set(
            name=f"{_SEND_DEDUP_PREFIX}{dedup_key}",
            value="1",
            nx=True,
            ex=_SEND_DEDUP_TTL_SECONDS,
        )
        return bool(ok)
    except Exception as e:  # pragma: no cover — best-effort
        log.warning(
            "send_dedup: Redis SETNX failed (%s) for key=%r; proceeding without claim",
            e,
            dedup_key,
        )
        return True


def _record_send_dedup_short_circuit(
    *,
    dedup_key: str,
    what: str,
    brand: str | None,
    template_name: str | None = None,
) -> None:
    """Emit the structured log line for a D-4 dedup short-circuit.

    Mirrors :func:`_record_dedup_short_circuit` (B-3 daily dedup) so the
    log-based metric scrapers can pick this up without code change. The
    metric name is intentionally distinct (``whatsapp_send_dedup_short_circuit_total``)
    so the two dedup systems can be observed independently.
    """
    log.info(
        "metric=whatsapp_send_dedup_short_circuit_total what=%s brand=%s template=%s",
        what,
        brand or "-",
        template_name or "-",
    )


def _redact_phone(phone: str | None) -> str:
    """Reduce a phone number to ``***<last 4 digits>`` for log lines.

    D-3: every per-failure log path used to emit the full ``to=<phone>``,
    which is PII under DPDP and can land in Sentry on dead-letter. We
    keep the last 4 digits so ops can correlate against a support ticket
    or Razorpay record without exposing the full number.

    A ``None`` phone returns ``"***"`` (constant placeholder). Inputs
    shorter than 4 digits return ``"***"`` rather than a partial leak.
    """
    if not phone:
        return "***"
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


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


def _today_ist() -> date:
    """Return the current IST date.

    Computed from UTC ``now()`` rather than ``datetime.now(_IST).date()``
    so the function is mock-friendly: tests patch
    ``whatsapp_delivery.dispatch.worker._utcnow`` to inject a deterministic
    moment, and the IST conversion happens through the public ``astimezone``
    path which respects the patched value.
    """
    return _utcnow().astimezone(_IST).date()


def _utcnow() -> datetime:
    """Wall-clock now in UTC.

    Wrapped so tests can monkey-patch one symbol to freeze time across
    every per-day-key computation in the worker.
    """
    return datetime.now(timezone.utc)


def _record_dedup_short_circuit(template_name: str, brand: str) -> None:
    """Bump the cross-pod dedup short-circuit metric.

    The worker module has no prom counter infrastructure of its own (see the
    audit's broader observability scope); for now we emit a structured log
    line that downstream log-based metrics can parse. The line shape is
    stable: ``metric=whatsapp_dedup_short_circuit_total
    template=<name> brand=<brand>``.

    A future enhancement should replace this with a
    ``prometheus_client.Counter`` when the package gains a metrics module;
    until then this is the lowest-coupling option.
    """
    log.info(
        "metric=whatsapp_dedup_short_circuit_total template=%s brand=%s",
        template_name,
        brand,
    )


def _claim_daily_send_slot(
    *,
    user_id: str,
    template_name: str,
    brand: str,
    rq_job_id: str | None,
) -> bool:
    """Try to claim the per-day dedup slot. Return True on first claim.

    Best-effort: failures here (DB unreachable, ORM model not registered,
    etc.) are logged and the worker proceeds with the send. The DB-side
    UNIQUE INDEX is the *guarantee* — this function is the optimization
    that avoids paying for a Meta call when we can detect the duplicate
    locally. A worker without DB access would still be safe because the
    producer-side ``log_send_enqueued`` INSERT would raise IntegrityError
    if a row already exists for today (the constraint is enforced at the
    DB level regardless of which code path inserts).

    Returns:
      True  — the calling worker now owns the daily slot; proceed to send.
      False — another pod already claimed; skip the Meta call.
    """
    if not user_id:
        # No user context (ad-hoc / test path). Treat as a successful claim
        # so we don't change behavior for callers that don't pass user_id.
        return True

    try:
        # Imported lazily so this module doesn't force callers to install
        # the data-access dependency for worker functions that don't dedup.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from data_access.engine import get_session
        from data_access.models import WhatsAppDeliveryLog
    except Exception as e:  # pragma: no cover — data-access optional in test
        log.warning(
            "dedup claim: data-access import failed (%s); proceeding without claim",
            e,
        )
        return True

    today_ist = _today_ist()

    try:
        with get_session() as s:
            dialect = s.get_bind().dialect.name
            insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
            # The partial unique index uses ``WHERE send_date_ist IS NOT
            # NULL``; both Postgres and SQLite require the upsert to
            # carry the same predicate via ``index_where`` for the
            # conflict-arbiter lookup to find it.
            stmt = (
                insert_fn(WhatsAppDeliveryLog)
                .values(
                    user_id=user_id,
                    template_name=template_name,
                    brand=brand,
                    rq_job_id=rq_job_id,
                    send_date_ist=today_ist,
                    delivery_status="pending",
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "template_name", "send_date_ist"],
                    index_where=WhatsAppDeliveryLog.send_date_ist.isnot(None),
                )
            )
            result = s.execute(stmt)
            # get_session() commits on context exit; ``rowcount`` is set
            # before the commit so we can read it here.
            return result.rowcount > 0
    except Exception as e:
        # B-3 fail-open: a DB hiccup MUST NOT block legitimate sends. The
        # UNIQUE INDEX still backstops at the DB layer for any path that
        # inserts via the ORM (producer-side log_send_enqueued).
        log.warning(
            "dedup claim: DB error (%s) on (user=%s, tmpl=%s); proceeding without claim",
            e,
            user_id,
            template_name,
        )
        return True


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
    dedup_key: str | None = None,
) -> str:
    """RQ entry: send a free-text message.

    D-4: when ``dedup_key`` is provided, the worker claims a Redis SETNX
    slot keyed by ``send_dedup:<dedup_key>`` (10-min TTL) before sending.
    A second invocation with the same dedup_key (e.g. RQ retry of an
    already-acknowledged enqueue) short-circuits without calling Meta.
    """
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        # D-3: never emit a full phone number into logs.
        log.warning("nowlez kill-switch on; skipping send_text to=%s", _redact_phone(to))
        return ""
    if dedup_key and not _claim_send_dedup_key(dedup_key):
        _record_send_dedup_short_circuit(
            dedup_key=dedup_key, what="send_text", brand=brand,
        )
        log.info(
            "send_dedup short-circuit: what=send_text key=%s to=%s",
            dedup_key,
            _redact_phone(to),
        )
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
            to=_redact_phone(to),
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
    dedup_per_day: bool = False,
    dedup_key: str | None = None,
) -> str:
    """RQ entry: send a registry-backed template by name + variable dict.

    Looks the template up in the registry, picks the variable ordering Meta
    expects from the template spec, optionally uploads a document header,
    and dispatches via :class:`TemplateClient`.

    B-3: when ``dedup_per_day`` is True and the template is in the
    :data:`_DEDUP_DAILY_TEMPLATES` allowlist, the worker first attempts to
    claim the ``(user_id, template_name, send_date_ist=today_IST)`` slot
    via INSERT ON CONFLICT DO NOTHING. If another pod already claimed,
    returns ``""`` without calling Meta and bumps the
    ``whatsapp_dedup_short_circuit_total`` metric. The default is False so
    transactional templates (signup welcome, OTP, order-uploaded, etc.)
    keep their current behavior.

    D-4: when ``dedup_key`` is provided, the worker also runs a Redis
    SETNX claim (``send_dedup:<dedup_key>``, 10-min TTL) and short-circuits
    on duplicate. Intended for transactional sends where the producer has
    a natural stable key (e.g. ``f"welcome:{user_id}"``). B-3 and D-4 use
    distinct key spaces and can be applied independently.
    """
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        # D-3: redact phone in kill-switch logs too.
        log.warning(
            "nowlez kill-switch on; skipping send_template name=%s to=%s",
            template_name,
            _redact_phone(to),
        )
        return ""

    # B-3: per-day dedup short-circuit. Only honored when the producer
    # explicitly opts in AND the template is in the allowlist, so a
    # mis-set kwarg can't silently silence a transactional send.
    if dedup_per_day and template_name in _DEDUP_DAILY_TEMPLATES and user_id:
        if not _claim_daily_send_slot(
            user_id=user_id,
            template_name=template_name,
            brand=brand,
            rq_job_id=_current_rq_job_id(),
        ):
            _record_dedup_short_circuit(template_name, brand)
            log.info(
                "dedup short-circuit: another pod claimed name=%s user=%s to=%s",
                template_name,
                user_id,
                _redact_phone(to),
            )
            return ""

    # D-4: producer-side idempotency for transactional sends. Runs AFTER
    # the daily-dedup check so a (daily + dedup_key) combination first
    # confirms the day-slot before claiming the Redis key.
    if dedup_key and not _claim_send_dedup_key(dedup_key):
        _record_send_dedup_short_circuit(
            dedup_key=dedup_key,
            what="send_template",
            brand=brand,
            template_name=template_name,
        )
        log.info(
            "send_dedup short-circuit: what=send_template name=%s key=%s to=%s",
            template_name,
            dedup_key,
            _redact_phone(to),
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
            to=_redact_phone(to),
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
    dedup_per_day: bool = False,
    dedup_key: str | None = None,
) -> str:
    """RQ entry: low-level template send (positional vars, pre-uploaded media).

    See :func:`_do_send_template` for the ``dedup_per_day`` and
    ``dedup_key`` semantics — both are honored here too so the two
    template entry points behave identically when the producer opts in.
    """
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        # D-3: redact phone in kill-switch logs too.
        log.warning(
            "nowlez kill-switch on; skipping send_template_with_components name=%s to=%s",
            template_name,
            _redact_phone(to),
        )
        return ""

    # B-3: same per-day dedup short-circuit as _do_send_template.
    if dedup_per_day and template_name in _DEDUP_DAILY_TEMPLATES and user_id:
        if not _claim_daily_send_slot(
            user_id=user_id,
            template_name=template_name,
            brand=brand,
            rq_job_id=_current_rq_job_id(),
        ):
            _record_dedup_short_circuit(template_name, brand)
            log.info(
                "dedup short-circuit: another pod claimed name=%s user=%s to=%s",
                template_name,
                user_id,
                _redact_phone(to),
            )
            return ""

    # D-4: producer-side idempotency, same shape as _do_send_template.
    if dedup_key and not _claim_send_dedup_key(dedup_key):
        _record_send_dedup_short_circuit(
            dedup_key=dedup_key,
            what="send_template_with_components",
            brand=brand,
            template_name=template_name,
        )
        log.info(
            "send_dedup short-circuit: what=send_template_with_components "
            "name=%s key=%s to=%s",
            template_name,
            dedup_key,
            _redact_phone(to),
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
            to=_redact_phone(to),
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
    dedup_key: str | None = None,
) -> str:
    """RQ entry: upload + send a PDF in one call.

    D-4: when ``dedup_key`` is provided, the worker claims a Redis SETNX
    slot before the upload (so a retry won't re-upload the same PDF
    either) and short-circuits on duplicate.
    """
    cfg = WhatsAppConfig()
    if brand == "nowlez" and cfg.whatsapp_nowlez_disabled:
        return ""
    if dedup_key and not _claim_send_dedup_key(dedup_key):
        _record_send_dedup_short_circuit(
            dedup_key=dedup_key, what="send_document", brand=brand,
        )
        log.info(
            "send_dedup short-circuit: what=send_document key=%s to=%s",
            dedup_key,
            _redact_phone(to),
        )
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
            to=_redact_phone(to),
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
