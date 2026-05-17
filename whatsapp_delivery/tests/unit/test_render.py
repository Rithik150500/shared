"""Tests for ``whatsapp_delivery.render.render_hybrid``.

The function picks between a text rendering and a PDF rendering based on
item count. The PDF path imports weasyprint lazily — weasyprint has
heavy system deps (Pango/Cairo) so we mock both ``jinja2.Template`` and
``weasyprint.HTML`` for the PDF assertions to keep the suite portable.

The text path is the hot path (small N) and is exercised end-to-end.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from whatsapp_delivery.render import render_hybrid


# ---------------------------------------------------------------------------
# Text path (N <= threshold).
# ---------------------------------------------------------------------------


def test_render_hybrid_below_threshold_returns_text():
    """N <= threshold → text body returned, text_fn invoked once."""
    items = ["one", "two", "three"]
    calls: list[list[str]] = []

    def text_fn(items_in: list[str]) -> str:
        calls.append(items_in)
        return "rendered text: " + ", ".join(items_in)

    result = render_hybrid(
        items=items,
        threshold=5,
        text_fn=text_fn,
        html_template_str="<unused>",
        ctx={},
    )

    assert result == {"mode": "text", "body": "rendered text: one, two, three"}
    # text_fn was called with the items list (and exactly once).
    assert calls == [items]


def test_render_hybrid_exactly_at_threshold_returns_text():
    """N == threshold is still <= threshold → text branch."""
    items = ["a", "b", "c"]
    result = render_hybrid(
        items=items,
        threshold=3,
        text_fn=lambda it: f"n={len(it)}",
        html_template_str="<unused>",
        ctx={},
    )
    assert result == {"mode": "text", "body": "n=3"}


def test_render_hybrid_empty_items_returns_text():
    """Edge case: 0 items still goes through the text path (0 <= threshold)."""
    result = render_hybrid(
        items=[],
        threshold=5,
        text_fn=lambda it: "(empty)",
        html_template_str="<unused>",
        ctx={},
    )
    assert result["mode"] == "text"
    assert result["body"] == "(empty)"


# ---------------------------------------------------------------------------
# PDF path (N > threshold).
#
# Stub jinja2 + weasyprint in sys.modules so the lazy import inside
# render_hybrid lands on our mocks and never tries to dlopen Pango on a
# system that doesn't have it.
# ---------------------------------------------------------------------------


def _install_pdf_stubs(monkeypatch, *, captured: dict):
    """Install fake jinja2 + weasyprint modules; record calls into ``captured``."""

    class FakeJinjaTemplate:
        def __init__(self, src: str) -> None:
            captured["jinja_src"] = src

        def render(self, **ctx) -> str:
            captured["jinja_ctx"] = ctx
            return f"<html>rendered for n={ctx.get('n')}</html>"

    jinja2_mod = types.ModuleType("jinja2")
    jinja2_mod.Template = FakeJinjaTemplate  # type: ignore[attr-defined]

    class FakeWeasyHTML:
        def __init__(self, *, string: str) -> None:
            captured["weasy_html"] = string

        def write_pdf(self) -> bytes:
            captured["weasy_called"] = True
            return b"%PDF-FAKE-BYTES"

    weasy_mod = types.ModuleType("weasyprint")
    weasy_mod.HTML = FakeWeasyHTML  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "jinja2", jinja2_mod)
    monkeypatch.setitem(sys.modules, "weasyprint", weasy_mod)


def test_render_hybrid_above_threshold_returns_pdf(monkeypatch):
    """N > threshold → Jinja2 renders → WeasyPrint writes PDF bytes."""
    captured: dict = {}
    _install_pdf_stubs(monkeypatch, captured=captured)

    items = list(range(20))
    result = render_hybrid(
        items=items,
        threshold=5,
        text_fn=lambda _: (_ for _ in ()).throw(  # type: ignore[arg-type]
            AssertionError("text_fn must NOT be called on PDF path"),
        ),
        html_template_str="<html>{{ n }}</html>",
        ctx={"n": len(items)},
    )

    assert result["mode"] == "pdf"
    assert result["pdf_bytes"] == b"%PDF-FAKE-BYTES"
    # Template source and ctx were threaded through.
    assert captured["jinja_src"] == "<html>{{ n }}</html>"
    assert captured["jinja_ctx"] == {"n": 20}
    # WeasyPrint received the rendered HTML.
    assert "<html>" in captured["weasy_html"]
    assert captured["weasy_called"] is True


def test_render_hybrid_pdf_path_skips_text_fn(monkeypatch):
    """text_fn must not be invoked on the PDF branch (it's documented as
    not being called above threshold — keeps PDF-heavy callers from
    importing/loading their text helpers when they don't have to)."""
    captured: dict = {}
    _install_pdf_stubs(monkeypatch, captured=captured)

    text_fn_called = MagicMock()
    render_hybrid(
        items=[1, 2, 3, 4, 5, 6],
        threshold=5,
        text_fn=text_fn_called,
        html_template_str="<html>{{ x }}</html>",
        ctx={"x": "value"},
    )
    text_fn_called.assert_not_called()
