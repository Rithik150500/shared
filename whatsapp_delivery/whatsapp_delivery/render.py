"""render_hybrid -- text or PDF based on item count threshold.

Many feature outputs are short for small N and unwieldy for large N (cause-list
digest, portfolio view). render_hybrid picks the right format:
- N <= threshold -> text returned directly
- N > threshold -> Jinja2 -> WeasyPrint -> PDF bytes

WeasyPrint is heavy (Pango/Cairo system deps). The import is done lazily so importing
this module doesn't crash on systems without the libs (e.g. CI runners that won't
exercise the PDF path).
"""
from __future__ import annotations

from typing import Any, Callable, TypedDict


class TextResult(TypedDict):
    mode: str  # "text"
    body: str


class PdfResult(TypedDict):
    mode: str  # "pdf"
    pdf_bytes: bytes


def render_hybrid(
    *,
    items: list[Any],
    threshold: int,
    text_fn: Callable[[list[Any]], str],
    html_template_str: str,
    ctx: dict[str, Any],
) -> TextResult | PdfResult:
    """Pick text or PDF based on item count.

    `text_fn` is called with `items` only when below threshold (keeps the WeasyPrint
    import lazy). `html_template_str` is rendered with `ctx` via Jinja2 when above.
    """
    if len(items) <= threshold:
        return {"mode": "text", "body": text_fn(items)}

    from jinja2 import Template
    from weasyprint import HTML

    html = Template(html_template_str).render(**ctx)
    pdf_bytes = HTML(string=html).write_pdf()
    return {"mode": "pdf", "pdf_bytes": pdf_bytes}
