"""Worker-side tests for D-5 URL-based document send.

Covers ``_resolve_document_url`` (fetches bytes from file:// or http(s)://)
and ``_do_send_document`` end-to-end with the new ``document_url`` kwarg.

The 16 MB cap (``_DOCUMENT_MAX_BYTES``) is enforced by the worker — a
malicious URL that returns gigabytes should be rejected before the bytes
are buffered into memory or shipped to Meta.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from whatsapp_delivery.dispatch import worker as w


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)


# ---------------------------------------------------------------------------
# _resolve_document_url — direct unit tests
# ---------------------------------------------------------------------------


def test_resolve_document_url_reads_file_scheme(tmp_path: Path):
    """file:///abs/path is read from disk verbatim."""
    pdf_path = tmp_path / "x.pdf"
    body = b"%PDF-1.4\n...payload..."
    pdf_path.write_bytes(body)

    out = w._resolve_document_url(f"file:///{pdf_path.as_posix().lstrip('/')}")
    assert out == body


def test_resolve_document_url_file_scheme_handles_url_encoding(tmp_path: Path):
    """Spaces and other reserved characters in the path are decoded."""
    pdf_path = tmp_path / "name with space.pdf"
    body = b"%PDF-1.4\n..."
    pdf_path.write_bytes(body)

    # URL-encode spaces as %20 — the helper must un-encode before reading.
    encoded = pdf_path.as_posix().replace(" ", "%20")
    out = w._resolve_document_url(f"file:///{encoded.lstrip('/')}")
    assert out == body


def test_resolve_document_url_file_scheme_nonexistent_raises(tmp_path: Path):
    """A missing local file raises FileNotFoundError (not ValueError)."""
    missing = tmp_path / "does-not-exist.pdf"
    with pytest.raises(FileNotFoundError):
        w._resolve_document_url(f"file:///{missing.as_posix().lstrip('/')}")


@respx.mock
def test_resolve_document_url_fetches_https_scheme():
    """https:// triggers an HTTP GET and returns the response body."""
    body = b"%PDF-1.4\n...remote..."
    respx.get("https://example.com/x.pdf").mock(
        return_value=Response(200, content=body)
    )
    out = w._resolve_document_url("https://example.com/x.pdf")
    assert out == body


@respx.mock
def test_resolve_document_url_fetches_http_scheme():
    """Plain http:// is also accepted (some intranet-style use cases)."""
    body = b"%PDF-1.4\n...intranet..."
    respx.get("http://internal/x.pdf").mock(
        return_value=Response(200, content=body)
    )
    out = w._resolve_document_url("http://internal/x.pdf")
    assert out == body


@respx.mock
def test_resolve_document_url_https_raises_on_oversized_response():
    """A 17 MB response trips the 16 MB cap and raises ValueError.

    The cap defends against a malicious URL that returns gigabytes; Meta's
    Cloud API documents a 100 MB limit but realistic order PDFs are
    sub-5 MB, so 16 MB is comfortable headroom without becoming a Redis
    or worker-memory liability.
    """
    oversize = b"x" * (16 * 1024 * 1024 + 1)
    respx.get("https://example.com/huge.pdf").mock(
        return_value=Response(200, content=oversize)
    )
    with pytest.raises(ValueError, match="max_bytes"):
        w._resolve_document_url("https://example.com/huge.pdf")


def test_resolve_document_url_file_scheme_raises_on_oversized_file(tmp_path: Path):
    """The same cap applies to file:// reads (defense-in-depth)."""
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="max_bytes"):
        w._resolve_document_url(f"file:///{big.as_posix().lstrip('/')}")


def test_resolve_document_url_rejects_unknown_scheme():
    """Unsupported URL schemes raise ValueError before any I/O."""
    with pytest.raises(ValueError, match="scheme"):
        w._resolve_document_url("ftp://example.com/x.pdf")


def test_resolve_document_url_rejects_no_scheme():
    """A bare path (no scheme) is also rejected — callers must be explicit."""
    with pytest.raises(ValueError, match="scheme"):
        w._resolve_document_url("/just/a/path.pdf")


def test_resolve_document_url_respects_custom_max_bytes(tmp_path: Path):
    """The helper accepts an override for the size cap (used in tests)."""
    body = b"x" * 1024  # 1 KB
    pdf = tmp_path / "small.pdf"
    pdf.write_bytes(body)

    # max_bytes=512 — file is 1 KB so this trips.
    with pytest.raises(ValueError, match="max_bytes"):
        w._resolve_document_url(
            f"file:///{pdf.as_posix().lstrip('/')}", max_bytes=512,
        )

    # max_bytes=2048 — 1 KB fits.
    out = w._resolve_document_url(
        f"file:///{pdf.as_posix().lstrip('/')}", max_bytes=2048,
    )
    assert out == body


# ---------------------------------------------------------------------------
# _do_send_document — end-to-end with URL parameter
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_document_resolves_file_url_then_uploads(tmp_path: Path):
    """A file:// URL is read off disk, then the bytes flow to Meta."""
    body = b"%PDF-1.4\n...from-disk..."
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(body)

    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-disk"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.disk"}]})
    )

    wamid = w._do_send_document(
        to="+919999999999",
        document_url=f"file:///{pdf.as_posix().lstrip('/')}",
        caption="Order PDF",
        filename="order.pdf",
        brand="munshi",
    )
    assert wamid == "wamid.disk"


@respx.mock
def test_do_send_document_resolves_https_url_then_uploads():
    """An https:// URL is GETted, then the bytes flow to Meta."""
    body = b"%PDF-1.4\n...from-net..."
    respx.get("https://cdn.example.com/orders/abc.pdf").mock(
        return_value=Response(200, content=body)
    )
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-net"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.net"}]})
    )

    wamid = w._do_send_document(
        to="+919999999999",
        document_url="https://cdn.example.com/orders/abc.pdf",
        caption="Order PDF",
        filename="order.pdf",
        brand="munshi",
    )
    assert wamid == "wamid.net"


@respx.mock
def test_do_send_document_propagates_resolve_failure(tmp_path: Path):
    """If URL resolution fails, the error propagates BEFORE the Meta call.

    This means RQ won't see a transient error and won't retry — a 404 on
    R2 isn't going to magically heal. The job ends up in the failed-job
    registry where ops can inspect it.
    """
    missing = tmp_path / "missing.pdf"
    # No media or messages route registered — if the worker tried to call
    # Meta despite the file being missing, respx would raise instead.
    with pytest.raises(FileNotFoundError):
        w._do_send_document(
            to="+919999999999",
            document_url=f"file:///{missing.as_posix().lstrip('/')}",
            caption="cap",
            filename="missing.pdf",
            brand="munshi",
        )


def test_do_send_document_nowlez_kill_switch_short_circuits_before_resolve(
    monkeypatch, tmp_path: Path,
):
    """Kill-switch short-circuit runs BEFORE URL resolution.

    A nowlez-disabled worker should never read the file or hit the network
    — return "" immediately. This keeps the kill switch cheap and avoids
    leaking I/O signals when the brand is disabled.
    """
    monkeypatch.setenv("WHATSAPP_NOWLEZ_DISABLED", "1")
    # Deliberately point at a nonexistent path: if the worker tried to
    # resolve, it would raise FileNotFoundError.
    missing = tmp_path / "should-not-be-read.pdf"
    out = w._do_send_document(
        to="+919999999999",
        document_url=f"file:///{missing.as_posix().lstrip('/')}",
        caption="cap",
        filename="x.pdf",
        brand="nowlez",
    )
    assert out == ""
