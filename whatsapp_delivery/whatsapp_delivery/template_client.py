"""Helpers for sending Meta UTILITY templates via the Cloud API.

UTILITY templates allow bots to send messages OUTSIDE the 24-hour customer-service
window. Their wording is filed in advance via WhatsApp Business Manager (see
deploy/templates_filed.yml). This module wraps MetaClient to construct the
template-component payload the API expects.

Behaviour per environment:
    - If `MetaClient.send_template` is overridden in tests, that wins.
    - In production, send_template POSTs the documented Graph API payload.
    - If `META_TEMPLATES_FALLBACK_TO_TEXT=1` is set in the environment, every
      send_template call falls through to send_text instead -- useful for local
      dev where templates aren't filed yet.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
)
from whatsapp_delivery.meta_client import META_GRAPH_API_VERSION


log = logging.getLogger(__name__)

# D-11: Graph API version is owned by meta_client.META_GRAPH_API_VERSION
# so the runtime send path and template path stay in lock-step.
_GRAPH_VERSION = META_GRAPH_API_VERSION
_GRAPH_BASE = f"https://graph.facebook.com/{_GRAPH_VERSION}"
_TIMEOUT = 30


# D-6: every variable that flows into a template component used to be
# interpolated via ``str(v)`` with no escape, no control-char filter, no
# RTL/bidi-override strip, and no ``{{N}}`` escape. A user-supplied case
# title containing a newline, NULL byte, right-to-left override codepoint,
# literal ``{{2}}`` placeholder, or a thousand-character paste could
# silently break Meta template validation OR inject extra placeholder
# slots that Meta would then attempt to fill.
#
# ``_sanitize_var`` is the single chokepoint: strip C0/C1 control chars
# (keep tab), strip RTL/LTR override + isolate codepoints, defuse the
# ``{{N}}`` placeholder pattern, and length-cap with a trailing ellipsis
# so the receiver sees data was elided rather than silent truncation.
#
# Tab (0x09) is intentionally kept because some templates legitimately
# use tab-aligned bodies; if Meta ever rejects tabs we can add it to the
# strip set without a behavior surprise for callers (the rendered text
# would just be slightly tighter). Newline (\\x0a) and carriage return
# (\\x0d) ARE stripped — Meta's template variable slots are single-line
# values and a user-supplied newline breaks rendering on the recipient
# side (and, in some cases, the template validation pre-send).
_BIDI_CHARS = (
    "‪‫‬‭‮"   # LRE RLE PDF LRO RLO
    "⁦⁧⁨⁩"          # LRI RLI FSI PDI
)
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_LITERAL_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
_DEFAULT_VAR_MAX_LEN = 256


def _sanitize_var(value: Any, max_len: int = _DEFAULT_VAR_MAX_LEN) -> str:
    """Make a template variable value safe to interpolate into Meta's payload.

    Order of operations:

    1. Coerce to ``str`` (preserves the original ``str(v)`` semantics).
    2. Strip C0/C1 control characters EXCEPT tab (\\x09) which some
       templates use for alignment.
    3. Strip RTL/LTR override + isolate codepoints (known phishing vector
       and a source of "the rendered string isn't what was typed" bugs).
    4. Defuse any literal ``{{N}}`` substrings the user typed so they
       can't masquerade as template placeholders Meta will try to fill.
    5. Length-cap to ``max_len`` (default 256) with a trailing ``…`` so
       the recipient sees that data was elided.
    """
    s = str(value)
    s = _CTRL_CHARS_RE.sub("", s)
    if any(ch in s for ch in _BIDI_CHARS):
        for ch in _BIDI_CHARS:
            s = s.replace(ch, "")
    # Replace ``{{N}}`` with ``{ {N} }`` — keeps the digits visible to
    # the recipient while breaking Meta's placeholder regex on both ends.
    s = _LITERAL_PLACEHOLDER_RE.sub(r"{ {\1} }", s)
    if len(s) > max_len:
        # -1 to leave room for the ellipsis; ``…`` is 1 visual char.
        s = s[: max_len - 1] + "…"
    return s


# D-9: ``META_TEMPLATES_FALLBACK_TO_TEXT=1`` is a dev/staging escape hatch
# that silently degrades every UTILITY template to plain text. Pre-fix this
# was a hidden state -- the only visible symptom was a wave of Meta 24h
# window errors days after the env var leaked into prod.
#
# Make it observable two ways:
#   * a once-per-process WARNING log (operators see it on pod startup if
#     the flag is set), and
#   * a counter in ``_METRICS["fallback_to_text_total"]`` that ticks up on
#     every fallback so the rate is visible even after the warning is gone.
#
# The counter dict shape mirrors what dispatch/worker.py emits via
# structured log lines today; a follow-up commit can promote both to a
# prometheus_client.Counter when the package gains a metrics module.
_FALLBACK_TO_TEXT_WARNED: bool = False
_METRICS: dict[str, int] = {"fallback_to_text_total": 0}


def _check_fallback_to_text() -> bool:
    """Return True if templates should silently degrade to plain text.

    On the first ``True`` return per process, emit a WARNING log. Every
    ``True`` return increments the ``fallback_to_text_total`` counter.
    """
    global _FALLBACK_TO_TEXT_WARNED
    on = os.environ.get("META_TEMPLATES_FALLBACK_TO_TEXT") == "1"
    if not on:
        return False
    if not _FALLBACK_TO_TEXT_WARNED:
        log.warning(
            "META_TEMPLATES_FALLBACK_TO_TEXT=1 is set; all template sends "
            "will degrade to plain text. This is for dev/staging only -- "
            "verify the env var is not leaking into prod."
        )
        _FALLBACK_TO_TEXT_WARNED = True
    _METRICS["fallback_to_text_total"] = _METRICS.get("fallback_to_text_total", 0) + 1
    # Mirror dispatch/worker.py's "metric=<name> ..." log convention so the
    # existing log-based metric scrapers can pick this up without code change.
    log.info("metric=whatsapp_template_fallback_to_text_total")
    return True


@dataclass
class TemplateClient:
    """Send pre-approved UTILITY templates via the Meta Cloud API.

    `send_template(to, name, variables)` constructs the body payload Meta expects
    (one body component with positional parameters) and posts it.
    """

    phone_number_id: str
    access_token: str

    def send_template(self, *, to: str, name: str, language: str, variables: list[str]) -> str:
        """Send a UTILITY template. Returns the wamid on success."""
        if _check_fallback_to_text():
            # Test / dev mode: fall back to free-text. Useful when templates haven't been
            # filed yet but we still want to exercise the notification pipeline.
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(_sanitize_var(v) for v in variables)
            return MetaClient(
                phone_number_id=self.phone_number_id,
                access_token=self.access_token,
            ).send_text(to, f"[{name}] {inline}")

        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": _sanitize_var(v)} for v in variables
                        ],
                    }
                ],
            },
        }

        return self._post(body, what="send_template")

    def send_template_with_document(
        self, *,
        to: str,
        name: str,
        language: str,
        variables: list[str],
        document_media_id: str,
    ) -> str:
        """Send a UTILITY template with a DOCUMENT header (PDF attached).

        The template MUST be filed in Business Manager with header_type=document
        (see `deploy/templates_filed.yml`'s `order_judgment_v1` for an example).
        Pass `document_media_id` from a prior `MetaClient.upload_media` call.

        Returns the wamid on success.
        """
        if _check_fallback_to_text():
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(_sanitize_var(v) for v in variables)
            return MetaClient(
                phone_number_id=self.phone_number_id,
                access_token=self.access_token,
            ).send_text(to, f"[{name}] {inline} (PDF stub: {document_media_id})")

        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "header",
                        "parameters": [
                            {"type": "document",
                             "document": {"id": document_media_id}},
                        ],
                    },
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": _sanitize_var(v)} for v in variables
                        ],
                    },
                ],
            },
        }

        return self._post(body, what="send_template_with_document")

    def send_template_with_components(
        self,
        *,
        to: str,
        name: str,
        language: str,
        body_variables: list[str],
        header_media_id: str | None = None,
        button_url_variables: list[str] | None = None,
    ) -> str:
        """Send a template with optional document header + URL-button variables.

        Spec §3.9: supports the full new_order shape (body + document header + URL
        button) used by Nowlez templates such as ``nowlez_new_order_v1``.

        - ``header_media_id`` -- optional Meta media_id from a prior upload_media
          call. If provided, a ``type=header`` component is included with a
          ``document`` parameter.
        - ``body_variables`` -- positional substitutions for the ``{{N}}`` body
          placeholders. Always emitted (even if empty, you'll get an empty body
          component with no parameters which Meta accepts for body-less templates).
        - ``button_url_variables`` -- variables for a single URL button at index 0
          (Meta's WhatsApp templates allow per-button URL suffixes). Optional.

        Returns the wamid on success.
        """
        if _check_fallback_to_text():
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(_sanitize_var(v) for v in body_variables)
            extras: list[str] = []
            if header_media_id:
                extras.append(f"PDF: {header_media_id}")
            if button_url_variables:
                extras.append("link: " + "/".join(_sanitize_var(v) for v in button_url_variables))
            suffix = f" ({'; '.join(extras)})" if extras else ""
            return MetaClient(
                phone_number_id=self.phone_number_id,
                access_token=self.access_token,
            ).send_text(to, f"[{name}] {inline}{suffix}")

        components: list[dict] = []

        if header_media_id:
            components.append({
                "type": "header",
                "parameters": [{"type": "document", "document": {"id": header_media_id}}],
            })

        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": _sanitize_var(v)} for v in body_variables],
        })

        if button_url_variables:
            components.append({
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": _sanitize_var(v)} for v in button_url_variables],
            })

        body: dict = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": components,
            },
        }

        return self._post(body, what="send_template_with_components")

    def _post(self, body: dict[str, Any], *, what: str) -> str:
        # Re-use MetaClient's shared error-mapping path so 429 / retry-able
        # error codes (D-1) and bearer-token sanitization (D-3) stay in
        # lock-step between the two clients.
        from whatsapp_delivery.meta_client import MetaClient

        url = f"{_GRAPH_BASE}/{self.phone_number_id}/messages"
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=_TIMEOUT,
        )
        MetaClient._raise_for_status(resp, what=what)
        return resp.json()["messages"][0]["id"]


# Backward-compat alias for callers that imported the class as MetaTemplateClient.
MetaTemplateClient = TemplateClient
