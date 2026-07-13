"""Tier-2 coverage gap: ``fetch_pdf`` (order + cause-list PDF GETs) bypassed the
shared rate gate -- a raw ``session.get`` with no ``wait()`` and no 405 back-off,
so the cause-list/order PDF burst could trip the per-IP throttle while the
JSON-API path was already paced by ``_session._send``. A 405 there also became a
plain ``PDFNotFound`` -- never counted, never penalized. These tests pin
``fetch_pdf`` onto the SAME shared schedule + AIMD 405 back-off + throttle
observability as the JSON path.
"""
from __future__ import annotations

import logging

import pytest
import requests

from ecourts_client import _session
from ecourts_client.errors import PDFNotFound
from ecourts_client.pdf import fetch_pdf


class _RecordingSession:
    """A ``requests.Session`` double: records call order, returns a canned resp."""

    def __init__(self, status_code: int, body: bytes, log: list[str]) -> None:
        self._status = status_code
        self._body = body
        self._log = log

    def get(self, *args, **kwargs):
        self._log.append("get")
        resp = requests.Response()
        resp.status_code = self._status
        resp._content = self._body
        return resp


def _noop_gate():
    return type("_G", (), {"wait": lambda self: None})()


def test_fetch_pdf_waits_on_shared_gate_before_the_get(monkeypatch):
    """fetch_pdf must reserve a slot on the shared rate gate BEFORE issuing the
    wire GET -- otherwise the PDF burst egress is uncapped on the shared IP."""
    order: list[str] = []

    class _Gate:
        def wait(self):
            order.append("wait")

    monkeypatch.setattr(_session, "_get_rate_gate", lambda: _Gate())
    sess = _RecordingSession(200, b"%PDF-1.4 body", order)

    out = fetch_pdf(sess, "https://csc.ecourts.gov.in/x.pdf")

    assert out.startswith(b"%PDF")
    assert order == ["wait", "get"]  # gate reserved BEFORE the wire call


def test_fetch_pdf_405_penalizes_shared_limiter_and_logs(monkeypatch, caplog):
    """A 405 (burst throttle) on a PDF GET must (a) widen the shared limiter so
    the whole fleet backs off and (b) emit the greppable ECOURTS_THROTTLE
    counter, exactly like the JSON path -- then surface as PDFNotFound."""
    penalized: list[bool] = []
    monkeypatch.setattr(_session, "_penalize_rate_gate", lambda: penalized.append(True))
    monkeypatch.setattr(_session, "_get_rate_gate", _noop_gate)
    sess = _RecordingSession(405, b"<html>Search Page not Found</html>", [])

    with caplog.at_level(logging.WARNING, logger="ecourts_client._session"):
        with pytest.raises(PDFNotFound):
            fetch_pdf(sess, "https://csc.ecourts.gov.in/x.pdf")

    assert penalized == [True]  # shared AIMD widened on the PDF 405
    assert "ECOURTS_THROTTLE" in caplog.text
    assert "kind=throttle_405" in caplog.text


def test_fetch_pdf_404_still_raises_without_penalizing(monkeypatch):
    """A genuine 404 (missing PDF) is NOT a throttle: it must not widen the
    shared limiter -- no false fleet-wide back-off on an ordinary missing file."""
    penalized: list[bool] = []
    monkeypatch.setattr(_session, "_penalize_rate_gate", lambda: penalized.append(True))
    monkeypatch.setattr(_session, "_get_rate_gate", _noop_gate)
    sess = _RecordingSession(404, b"not found", [])

    with pytest.raises(PDFNotFound):
        fetch_pdf(sess, "https://csc.ecourts.gov.in/missing.pdf")
    assert penalized == []  # 404 != throttle
