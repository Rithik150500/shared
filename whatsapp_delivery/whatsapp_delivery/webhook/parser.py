"""Meta webhook payload parsing: incoming user messages and delivery receipts.

``parse_incoming`` walks the ``{entry: [{changes: [{value: {messages: [...]}}]}]}``
payload and returns a list of typed ``IncomingMessage`` records, dropping
non-message events (delivery statuses, read receipts, etc.).

``parse_status_updates`` is the dual: it extracts the ``value.statuses`` entries
(sent / delivered / read / failed) for the WhatsApp delivery-log pipeline
(spec §4.7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IncomingButton:
    button_id: str
    title: str | None


@dataclass(frozen=True)
class IncomingMedia:
    media_id: str
    mime_type: str
    sha256: str | None = None


@dataclass(frozen=True)
class IncomingMessage:
    meta_message_id: str
    from_phone: str  # E.164 with leading '+', e.g. '+919876543210'
    timestamp: int   # epoch seconds
    type: str        # 'text' | 'image' | 'document' | 'audio' | 'video' | 'interactive' | 'button'
    text: str | None = None
    media: IncomingMedia | None = None
    button: IncomingButton | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryStatus:
    """One Meta delivery / read receipt for an outbound message.

    Per spec §4.7, drives the whatsapp_delivery_log table updates so the
    Nowlez UI can show send-state per case event.
    """
    meta_message_id: str
    recipient_id: str  # phone digits (no leading '+')
    status: str        # 'sent' | 'delivered' | 'read' | 'failed'
    timestamp: int
    failure_reason: str | None = None
    error_code: int | None = None  # Meta numeric error code (e.g. 131026)


def parse_incoming(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Walk the Meta webhook payload and extract user-sent messages.

    Skips delivery statuses and read receipts (those land under `value.statuses`).
    """
    out: list[IncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                out.append(_one_message(m))
    return out


def parse_status_updates(payload: dict[str, Any]) -> list[DeliveryStatus]:
    """Extract delivery + read receipts from a Meta webhook payload.

    Walks ``entry[].changes[].value.statuses[]`` and builds one ``DeliveryStatus``
    per receipt. The optional ``errors`` array (present on ``status == 'failed'``)
    is collapsed into ``failure_reason`` -- preferring the ``title`` field, then
    ``message``.
    """
    out: list[DeliveryStatus] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for s in change.get("value", {}).get("statuses", []):
                err = (s.get("errors") or [{}])[0]
                raw_code = err.get("code")
                out.append(DeliveryStatus(
                    meta_message_id=s["id"],
                    recipient_id=s["recipient_id"],
                    status=s["status"],
                    timestamp=int(s["timestamp"]),
                    failure_reason=err.get("title") or err.get("message"),
                    error_code=int(raw_code) if raw_code is not None else None,
                ))
    return out


def _one_message(m: dict[str, Any]) -> IncomingMessage:
    msg_type = m["type"]

    text = m.get("text", {}).get("body") if msg_type == "text" else None

    media: IncomingMedia | None = None
    if msg_type in ("image", "document", "audio", "video"):
        media_obj = m.get(msg_type, {})
        media = IncomingMedia(
            media_id=media_obj.get("id", ""),
            mime_type=media_obj.get("mime_type", ""),
            sha256=media_obj.get("sha256"),
        )

    button: IncomingButton | None = None
    if msg_type == "interactive":
        kind = m["interactive"]["type"]
        if kind == "button_reply":
            br = m["interactive"]["button_reply"]
            button = IncomingButton(button_id=br["id"], title=br.get("title"))
        elif kind == "list_reply":
            lr = m["interactive"]["list_reply"]
            button = IncomingButton(button_id=lr["id"], title=lr.get("title"))
    elif msg_type == "button":
        # Template-button reply: payload + text
        button = IncomingButton(
            button_id=m["button"].get("payload", ""),
            title=m["button"].get("text"),
        )

    raw_from = m["from"]
    from_phone = raw_from if raw_from.startswith("+") else "+" + raw_from

    return IncomingMessage(
        meta_message_id=m["id"],
        from_phone=from_phone,
        timestamp=int(m["timestamp"]),
        type=msg_type,
        text=text,
        media=media,
        button=button,
        raw=m,
    )
