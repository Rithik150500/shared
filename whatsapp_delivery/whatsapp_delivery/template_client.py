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

import os
from dataclasses import dataclass
from typing import Any

import httpx

from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
)


_GRAPH_VERSION = "v20.0"
_GRAPH_BASE = f"https://graph.facebook.com/{_GRAPH_VERSION}"
_TIMEOUT = 30


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
        if os.environ.get("META_TEMPLATES_FALLBACK_TO_TEXT") == "1":
            # Test / dev mode: fall back to free-text. Useful when templates haven't been
            # filed yet but we still want to exercise the notification pipeline.
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(str(v) for v in variables)
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
                            {"type": "text", "text": str(v)} for v in variables
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
        if os.environ.get("META_TEMPLATES_FALLBACK_TO_TEXT") == "1":
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(str(v) for v in variables)
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
                            {"type": "text", "text": str(v)} for v in variables
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
        if os.environ.get("META_TEMPLATES_FALLBACK_TO_TEXT") == "1":
            from whatsapp_delivery.meta_client import MetaClient
            inline = " ".join(str(v) for v in body_variables)
            extras: list[str] = []
            if header_media_id:
                extras.append(f"PDF: {header_media_id}")
            if button_url_variables:
                extras.append("link: " + "/".join(str(v) for v in button_url_variables))
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
            "parameters": [{"type": "text", "text": str(v)} for v in body_variables],
        })

        if button_url_variables:
            components.append({
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": str(v)} for v in button_url_variables],
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
