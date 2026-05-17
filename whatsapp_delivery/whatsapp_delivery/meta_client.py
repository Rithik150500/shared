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
- 400 with code 131047 -> Meta24HourWindowExpired (only templates allowed)
- other 4xx -> MetaInvalidMessage (do not retry)
"""
from __future__ import annotations

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
        """
        if resp.status_code >= 500:
            raise MetaTransientError(f"{what} {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                raise MetaInvalidMessage(f"{what} {resp.status_code}: {resp.text}")
            err = data.get("error", {})
            if err.get("code") == 131047:
                raise Meta24HourWindowExpired(err.get("message", "24h window expired"))
            raise MetaInvalidMessage(err.get("message", resp.text))
