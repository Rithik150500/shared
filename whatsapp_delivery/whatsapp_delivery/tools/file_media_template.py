"""File a single WhatsApp *media-header* message template with Meta.

The batch tool :mod:`whatsapp_delivery.tools.submit_templates_to_meta`
deliberately SKIPS templates whose HEADER format is DOCUMENT/IMAGE/VIDEO,
logging *"file manually via Meta dashboard"* — because a media-header
template create requires a separately-uploaded **sample asset**
(``example.header_handle``) that the batch tool never produces.

This module fills that gap. It implements Meta's **resumable upload**
protocol (rooted at the *app* id, not the phone-number id) to turn a
local media file into a ``header_handle``, then POSTs the template to
``/{WABA_ID}/message_templates`` with that handle attached.

Why a separate API from ``meta_client.upload_media``:
    ``upload_media`` hits ``POST /{phone_number_id}/media`` and returns a
    ``media_id`` used at **send** time. A template **create** will not
    accept a ``media_id`` — it needs the resumable-upload ``header_handle``
    (a different endpoint, the ``OAuth`` auth scheme, and the *app* id).

Resumable upload (3 steps):
    1. ``POST /{APP_ID}/uploads?file_name=&file_length=&file_type=``
       (Bearer token)                       -> ``{"id": "upload:<session>"}``
    2. ``POST /{upload:session}`` with header ``Authorization: OAuth <token>``
       and ``file_offset: 0`` + raw bytes    -> ``{"h": "<header_handle>"}``
    3. ``POST /{WABA_ID}/message_templates`` with the handle in
       ``components[HEADER].example.header_handle``.

Usage (dry-run prints the would-be create payload, no network for the
create; ``--dry-run`` does NOT upload either):

    python -m whatsapp_delivery.tools.file_media_template \\
        --name munshi_welcome_video_v1 --category MARKETING --language en \\
        --header-format video --media-file /tmp/munshi_welcome.mp4 \\
        --body "Hi {{1}}, welcome to Munshi ..." --body-example "Rahul" \\
        --footer "Reply STOP to unsubscribe." \\
        --button-quick-reply "Get Started" \\
        --dry-run

Real filing additionally requires ``--yes`` and the env/flags for
credentials (``META_ACCESS_TOKEN``, ``META_WABA_ID``, ``META_APP_ID``).

``httpx`` is imported lazily inside the network functions so the payload
builder (:func:`build_components`) stays unit-testable without it.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from whatsapp_delivery.meta_client import META_GRAPH_API_VERSION

_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
_TIMEOUT = 120  # uploads can be slow on a small droplet

log = logging.getLogger("file_media_template")

_MEDIA_FORMATS = {"VIDEO", "IMAGE", "DOCUMENT"}


# ---------------------------------------------------------------------------
# Payload building (pure — no network, no httpx)
# ---------------------------------------------------------------------------


def build_components(
    *,
    header_format: str,
    body_text: str,
    body_examples: list[str] | None = None,
    header_text: str | None = None,
    header_handle: str | None = None,
    footer_text: str | None = None,
    quick_reply: str | None = None,
    url_button: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build Meta's ``components`` array for a template create.

    ``header_format`` is one of TEXT/VIDEO/IMAGE/DOCUMENT (case-insensitive).
    For a media header pass ``header_handle`` (the resumable-upload handle);
    for a TEXT header pass ``header_text``. ``body_examples`` are positional
    sample values for ``{{1}}..{{N}}`` (required by Meta when the body has
    variables). At most one button is emitted (quick-reply or URL).
    """
    fmt = header_format.upper()
    components: list[dict[str, Any]] = []

    header: dict[str, Any] = {"type": "HEADER", "format": fmt}
    if fmt == "TEXT":
        header["text"] = header_text or ""
    elif fmt in _MEDIA_FORMATS:
        # Meta requires a sample asset handle so the reviewer can preview
        # the header. Omit only in dry-run, where the create never runs.
        if header_handle:
            header["example"] = {"header_handle": [header_handle]}
    else:
        raise ValueError(f"unsupported header format: {header_format!r}")
    components.append(header)

    body: dict[str, Any] = {"type": "BODY", "text": body_text}
    if body_examples:
        # body_text example is a list-of-rows; one row of positional values.
        body["example"] = {"body_text": [list(body_examples)]}
    components.append(body)

    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    if quick_reply and url_button:
        raise ValueError("pass only one of quick_reply / url_button")
    if quick_reply:
        components.append({
            "type": "BUTTONS",
            "buttons": [{"type": "QUICK_REPLY", "text": quick_reply}],
        })
    elif url_button:
        text, url = url_button
        components.append({
            "type": "BUTTONS",
            "buttons": [{"type": "URL", "text": text, "url": url}],
        })

    return components


def build_create_body(
    *, name: str, category: str, language: str,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": name,
        "category": category.upper(),
        "language": language,
        "components": components,
    }


# ---------------------------------------------------------------------------
# Meta API interaction (lazy httpx)
# ---------------------------------------------------------------------------


def upload_sample_media(
    file_path: Path, *, app_id: str, access_token: str,
    file_type: str | None = None,
) -> str:
    """Run Meta's 3-step resumable upload; return the ``header_handle``."""
    import httpx

    data = file_path.read_bytes()
    file_len = len(data)
    ftype = file_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    log.info("Resumable upload: %s (%d bytes, %s)", file_path.name, file_len, ftype)

    # Step 1: open an upload session. Pass the token in the Authorization
    # header (NOT a query param) so it never lands in request logs / proxies.
    r1 = httpx.post(
        f"{_GRAPH_BASE}/{app_id}/uploads",
        params={
            "file_name": file_path.name,
            "file_length": file_len,
            "file_type": ftype,
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT,
    )
    if r1.status_code >= 400:
        raise RuntimeError(f"upload-session create failed {r1.status_code}: {r1.text[:500]}")
    session_id = r1.json()["id"]  # "upload:<...>"
    log.info("Upload session: %s", session_id)

    # Step 2: stream the bytes from offset 0 (OAuth auth, file_offset header).
    r2 = httpx.post(
        f"{_GRAPH_BASE}/{session_id}",
        headers={"Authorization": f"OAuth {access_token}", "file_offset": "0"},
        content=data,
        timeout=_TIMEOUT,
    )
    if r2.status_code >= 400:
        raise RuntimeError(f"upload bytes failed {r2.status_code}: {r2.text[:500]}")
    handle = r2.json()["h"]
    log.info("Got header_handle (%d chars)", len(handle))
    return handle


def create_template(
    body: dict[str, Any], *, waba_id: str, access_token: str,
) -> dict[str, Any]:
    """POST the template create; raise with Meta's error body on non-2xx."""
    import httpx

    resp = httpx.post(
        f"{_GRAPH_BASE}/{waba_id}/message_templates",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"raw": resp.text[:500]}
        raise RuntimeError(f"Meta {resp.status_code}: {err}")
    return resp.json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_url_button(spec: str | None) -> tuple[str, str] | None:
    if not spec:
        return None
    # format: "Text::https://url"
    text, _, url = spec.partition("::")
    if not url:
        raise SystemExit("--button-url must be 'Text::https://url'")
    return (text, url)


def _resolve_text(inline: str | None, file_path: str | None, *, what: str) -> str | None:
    """Return ``inline`` if given, else the UTF-8 contents of ``file_path``.

    Reading long/non-ASCII copy from a file avoids shell-quoting and locale
    mangling when the CLI is driven over ssh -> docker exec.
    """
    if inline is not None and file_path:
        raise SystemExit(f"pass only one of --{what} / --{what}-file")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").rstrip("\n")
    return inline


def run(args: argparse.Namespace) -> int:
    args.body = _resolve_text(args.body, args.body_file, what="body")
    args.footer = _resolve_text(args.footer, args.footer_file, what="footer")
    if not args.body:
        log.error("--body or --body-file is required")
        return 2
    header_format = args.header_format.upper()
    media_needed = header_format in _MEDIA_FORMATS

    if media_needed and not args.media_file:
        log.error("--media-file is required for a %s header", header_format)
        return 2
    media_path = Path(args.media_file) if args.media_file else None
    if media_path and not media_path.is_file():
        log.error("media file not found: %s", media_path)
        return 2

    # Build the payload with a placeholder handle first (so dry-run can show it).
    components_preview = build_components(
        header_format=header_format,
        body_text=args.body,
        body_examples=args.body_example.split(",") if args.body_example else None,
        header_text=args.header_text,
        header_handle="<uploaded-at-runtime>" if media_needed else None,
        footer_text=args.footer,
        quick_reply=args.button_quick_reply,
        url_button=_parse_url_button(args.button_url),
    )
    preview_body = build_create_body(
        name=args.name, category=args.category, language=args.language,
        components=components_preview,
    )
    log.info("Create payload (preview):\n%s", json.dumps(preview_body, ensure_ascii=False, indent=2))

    if args.dry_run:
        log.info("DRY-RUN: no upload, no create. Re-run with --yes to file.")
        return 0

    if not args.yes:
        log.error("Refusing to file without --yes (this creates a real template on Meta).")
        return 2

    token = args.token or os.environ.get("META_ACCESS_TOKEN")
    waba_id = args.waba_id or os.environ.get("META_WABA_ID")
    app_id = args.app_id or os.environ.get("META_APP_ID")
    if not token or not waba_id:
        log.error("META_ACCESS_TOKEN and META_WABA_ID required (env or --token/--waba-id)")
        return 2
    if media_needed and not app_id:
        log.error("META_APP_ID required for a media header (env or --app-id)")
        return 2

    handle = None
    if media_needed:
        handle = upload_sample_media(
            media_path, app_id=app_id, access_token=token,
            file_type=args.media_type,
        )

    components = build_components(
        header_format=header_format,
        body_text=args.body,
        body_examples=args.body_example.split(",") if args.body_example else None,
        header_text=args.header_text,
        header_handle=handle,
        footer_text=args.footer,
        quick_reply=args.button_quick_reply,
        url_button=_parse_url_button(args.button_url),
    )
    create_body = build_create_body(
        name=args.name, category=args.category, language=args.language,
        components=components,
    )
    resp = create_template(create_body, waba_id=waba_id, access_token=token)
    log.info(
        "FILED %s [%s] -> id=%s status=%s category=%s",
        args.name, args.language, resp.get("id"),
        resp.get("status"), resp.get("category"),
    )
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="file_media_template")
    p.add_argument("--name", required=True)
    p.add_argument("--category", default="MARKETING")
    p.add_argument("--language", default="en")
    p.add_argument("--header-format", default="video",
                   help="video|image|document|text")
    p.add_argument("--header-text", default=None, help="for a TEXT header")
    p.add_argument("--media-file", default=None, help="local path to sample asset")
    p.add_argument("--media-type", default=None, help="override MIME, e.g. video/mp4")
    p.add_argument("--body", default=None)
    p.add_argument("--body-file", default=None,
                   help="read body from a UTF-8 file (avoids shell quoting)")
    p.add_argument("--body-example", default=None,
                   help="comma-separated positional examples for {{1}}..{{N}}")
    p.add_argument("--footer", default=None)
    p.add_argument("--footer-file", default=None, help="read footer from a UTF-8 file")
    p.add_argument("--button-quick-reply", default=None, help="quick-reply button text")
    p.add_argument("--button-url", default=None, help="'Text::https://url'")
    p.add_argument("--app-id", default=None, help="env META_APP_ID fallback")
    p.add_argument("--waba-id", default=None, help="env META_WABA_ID fallback")
    p.add_argument("--token", default=None, help="env META_ACCESS_TOKEN fallback")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="confirm real filing")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs full request URLs at INFO; silence it so a token in any
    # query string can never leak into logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run(_build_parser().parse_args(list(argv) if argv is not None else None))


if __name__ == "__main__":
    sys.exit(main())
