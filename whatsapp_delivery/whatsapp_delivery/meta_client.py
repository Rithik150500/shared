"""Meta WhatsApp Cloud API client (Graph v20+).

Surface area today:
- send_text                  free-text body
- send_interactive_buttons   <=3 reply buttons
- send_interactive_list      tap-to-pick list
- upload_media               POST {phone_id}/media (multipart) -> media_id
- send_document              type=document referencing a media_id
- send_document_from_bytes   convenience: upload + send in one call

Why two-step (upload then send) instead of `link`-based send?
WhatsApp's `link` mode requires the document to be fetched from a public URL,
which would either expose court PDFs on our infrastructure or require signed
URLs we'd have to build. The media-upload path keeps PDFs inside Meta's
30-day media TTL and only the recipient downloads them.

Error mapping:
- 5xx -> MetaTransientError (retryable)
- 429, or 400 with one of {130429, 131056, 133016} -> MetaTransientError
  (Meta's documented "retry after a delay" envelopes; D-1 audit fix)
- 400 with code 131047 -> Meta24HourWindowExpired (only templates allowed)
- other 4xx -> MetaInvalidMessage (do not retry)
"""
from __future__ import annotations

import re
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
# Meta caps document filename and caption lengths; clip defensively.
_MAX_DOCUMENT_FILENAME = 240
_MAX_DOCUMENT_CAPTION = 1024

# D-1: Meta documents these error codes as "retryable after a delay" — they
# come back in a 400-shaped envelope but the right behavior is to re-enqueue,
# not dead-letter as invalid.
#   130429 — application-level rate limit
#   131056 — pair (phone-pair) rate limit
#   133016 — temporary registration / messaging unavailable
_META_RETRYABLE_ERROR_CODES: frozenset[int] = frozenset({130429, 131056, 133016})

# D-3: error envelopes from Meta can echo our Authorization header on upload
# endpoints, and verbose bodies blow out logs. We truncate aggressively and
# strip anything that looks like a bearer token / JWT before raising.
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
_AUTH_HEADER_PATTERN = re.compile(r"Authorization\s*:\s*[^\s,]+", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+")
_MAX_META_ERROR_BODY = 200


def _sanitize_meta_response(text: str) -> str:
    """Redact bearer tokens / JWTs from a Meta error body, then truncate.

    Meta's media-upload endpoints occasionally echo the Authorization header
    back in error responses (especially on auth-related 4xx). Surface the
    body for debugging, but never leak the token. Order matters: redact
    *before* truncating so a token spanning the truncation point still gets
    scrubbed.
    """
    if not text:
        return ""
    scrubbed = _AUTH_HEADER_PATTERN.sub("Authorization: <redacted>", text)
    scrubbed = _BEARER_PATTERN.sub("Bearer <redacted>", scrubbed)
    scrubbed = _JWT_PATTERN.sub("<redacted-jwt>", scrubbed)
    return scrubbed[:_MAX_META_ERROR_BODY]


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a numeric ``Retry-After`` header value to seconds, or None."""
    if not value:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        # Meta could also send an HTTP-date but we don't currently honor that.
        return None


@dataclass
class MetaClient:
    phone_number_id: str
    access_token: str

    def send_text(self, to: str, body: str) -> str:
        """Send a free-text message. Returns the wamid (Meta's message id) on success."""
        return self._send({
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "text",
            "text": {"body": body},
        })

    def send_interactive_buttons(
        self,
        to: str,
        *,
        body: str,
        buttons: list[dict[str, str]],
    ) -> str:
        """Send up to 3 reply buttons. Each button is ``{'id': ..., 'title': ...}``.

        Meta caps button rows at 3 and titles at 20 chars; we truncate
        defensively. Returns the wamid on success.
        """
        clipped = [
            {"type": "reply", "reply": {"id": b["id"][:256], "title": b["title"][:20]}}
            for b in buttons[:3]
        ]
        return self._send({
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": clipped},
            },
        })

    def send_interactive_list(
        self,
        to: str,
        *,
        body: str,
        button_label: str,
        section_title: str,
        rows: list[dict[str, str]],
    ) -> str:
        """Send an interactive list: a tap-to-pick menu with up to 10 rows.

        Each row is {'id': ..., 'title': ..., 'description': ...?}. Title <= 24 chars,
        description <= 72 chars per Meta's API contract -- we truncate defensively.
        Returns the wamid on success.
        """
        # Meta caps lists at 10 rows; truncate hard to avoid 4xx.
        clipped: list[dict[str, str]] = []
        for r in rows[:10]:
            entry = {"id": r["id"][:200], "title": r["title"][:24]}
            if r.get("description"):
                entry["description"] = r["description"][:72]
            clipped.append(entry)
        return self._send({
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:1024]},
                "action": {
                    "button": button_label[:20],
                    "sections": [{"title": section_title[:24], "rows": clipped}],
                },
            },
        })

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two-step inbound media fetch. Returns ``(payload_bytes, mime_type)``.

        Meta's media endpoint returns a metadata envelope first
        (``{url, mime_type, sha256, file_size, ...}``) -- the actual binary
        lives behind the returned URL, fetched with the same bearer token.
        Both steps share the standard error mapping so a 5xx anywhere in
        the chain raises ``MetaTransientError`` and a 4xx raises
        ``MetaInvalidMessage``.

        Used by the QR-image handler to pull a user-sent image from Meta's
        media store for local pyzbar decoding. Sync, like every other
        ``MetaClient`` method -- decode + lookup happen on the dispatch
        path, and a 1MB phone JPEG decodes in <300ms.
        """
        # Step 1: fetch the metadata envelope.
        meta_url = f"{_GRAPH_BASE}/{media_id}"
        meta_resp = httpx.get(
            meta_url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=_TIMEOUT,
        )
        self._raise_for_status(meta_resp, what="download_media (metadata)")
        meta_payload = meta_resp.json()
        url = meta_payload.get("url")
        mime_type = meta_payload.get("mime_type", "")
        if not url:
            raise MetaInvalidMessage(
                f"download_media: metadata envelope missing 'url': {meta_payload!r}"
            )

        # Step 2: fetch the actual binary. The CDN URL still demands the
        # bearer token (Meta's lookaside doesn't accept anonymous fetches
        # for WhatsApp media).
        bin_resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=_TIMEOUT,
        )
        self._raise_for_status(bin_resp, what="download_media (binary)")
        return (bin_resp.content, mime_type)

    def upload_media(
        self,
        *,
        data: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        """Upload media to Meta's media store and return the resulting media_id.

        The id is valid for ~30 days and can be reused across multiple
        send_document/send_image/send_video calls -- e.g. one digest PDF
        broadcast to many users from a single upload.

        Note: this hits a different endpoint (`/media`, multipart) from the
        usual JSON `/messages` send, so it shares no path with `_send`.
        """
        url = f"{_GRAPH_BASE}/{self.phone_number_id}/media"
        # httpx treats tuples as (filename, fileobj, content_type) for `files=`;
        # multi-part text fields are sent via `data=`. Meta wants both
        # `messaging_product` and `type` alongside the binary payload.
        files: dict[str, Any] = {
            "file": (filename, data, mime_type),
        }
        form: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "type": mime_type,
        }
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            data=form,
            files=files,
            timeout=_TIMEOUT,
        )
        self._raise_for_status(resp, what="upload_media")
        return resp.json()["id"]

    def send_document(
        self,
        to: str,
        *,
        media_id: str,
        filename: str | None = None,
        caption: str | None = None,
    ) -> str:
        """Send a previously-uploaded document by media_id. Returns the wamid.

        `filename` is what the recipient sees on the file tile; without it
        the recipient sees a generic name. `caption` shows below the tile
        and supports basic WhatsApp formatting (*bold*, _italic_).
        """
        document: dict[str, Any] = {"id": media_id}
        if filename:
            document["filename"] = filename[:_MAX_DOCUMENT_FILENAME]
        if caption:
            document["caption"] = caption[:_MAX_DOCUMENT_CAPTION]
        return self._send({
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "document",
            "document": document,
        })

    def send_document_from_bytes(
        self,
        to: str,
        *,
        data: bytes,
        filename: str,
        mime_type: str = "application/pdf",
        caption: str | None = None,
    ) -> str:
        """Upload + send in one call. Returns the wamid.

        Use this for the common "render a PDF, send it" path. Re-use
        `upload_media` + `send_document` separately if you'll broadcast the
        same media to multiple recipients (one upload, many sends).
        """
        media_id = self.upload_media(data=data, filename=filename, mime_type=mime_type)
        return self.send_document(
            to, media_id=media_id, filename=filename, caption=caption,
        )

    def _send(self, body: dict[str, Any]) -> str:
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
        self._raise_for_status(resp, what="send")
        return resp.json()["messages"][0]["id"]

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, what: str) -> None:
        """Map a Meta Graph response to our typed exceptions.

        Centralized so upload_media + the JSON `_send` path share identical
        error semantics. The only path-specific behavior was the 131047
        24-hour-window code, which only fires on /messages -- safe to apply
        uniformly because /media never returns that code.

        D-1: 429 (HTTP rate-limited) and Meta's documented retry-able error
        codes (130429, 131056, 133016) raise MetaTransientError so RQ will
        re-enqueue. ``Retry-After`` is surfaced on the exception for the
        retry policy to honor.

        D-3: bodies are run through ``_sanitize_meta_response`` so a leaked
        bearer/Authorization in the upstream payload doesn't escape into
        Sentry.
        """
        if resp.status_code >= 500:
            raise MetaTransientError(
                f"{what} {resp.status_code}: {_sanitize_meta_response(resp.text)}",
                retry_after_seconds=_parse_retry_after(resp.headers.get("Retry-After")),
            )
        # 429 = rate-limited; always retryable. Check before the generic >=400
        # branch so the explicit retry path wins.
        if resp.status_code == 429:
            raise MetaTransientError(
                f"{what} 429: {_sanitize_meta_response(resp.text)}",
                retry_after_seconds=_parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                raise MetaInvalidMessage(
                    f"{what} {resp.status_code}: {_sanitize_meta_response(resp.text)}"
                )
            err = data.get("error", {})
            err_code = err.get("code")
            # D-1: a 4xx envelope with a documented retry-able code is still
            # retryable. Check before the 131047 / generic-invalid branches
            # so we don't dead-letter a rate-limit hit.
            if err_code in _META_RETRYABLE_ERROR_CODES:
                raise MetaTransientError(
                    f"{what} {resp.status_code} code={err_code}: "
                    f"{_sanitize_meta_response(err.get('message', '') or resp.text)}",
                    retry_after_seconds=_parse_retry_after(resp.headers.get("Retry-After")),
                )
            if err_code == 131047:
                raise Meta24HourWindowExpired(err.get("message", "24h window expired"))
            raise MetaInvalidMessage(
                _sanitize_meta_response(err.get("message", "") or resp.text)
            )
