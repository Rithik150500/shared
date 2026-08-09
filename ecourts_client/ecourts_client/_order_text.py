"""Normalise order text scraped out of a portal's HTML order sheet.

One definition, shared by both producers of ``OrderRef.order_text``
(``consumer.parsers._html_order_text`` and ``tribunal.kinds.tdsat``), because
this text is the ONLY copy of an HTML-only order — the portal serves no PDF and
no re-fetchable URL — and the consumer renders a document straight from it.

Three things happen here, in this order:

1. **Strip server-side error banners.** Court portals sometimes render a PHP
   error into the page body, after the order. tdsat.gov.in's ``orderp.php`` is
   the live example: dompdf raises mid-request, so every TDSAT order sheet ends
   with a ``Fatal error`` banner and stack trace — HTTP 200 throughout, so there
   is nothing to detect at the transport layer. It reached users: casepilot
   renders ``order_text`` into a PDF and 17 documents shipped with "…thrown in
   /home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php on line 313" printed
   at the bottom.

2. **Normalise whitespace, LINE STRUCTURE PRESERVED.** These pages are tables —
   one ``<td>`` per line — so the breaks carry the court's own layout (header,
   bench, case number, parties, then the order). Space-joining them yields a wall
   of text nobody can read. Horizontal runs collapse per line; blank-line runs
   squeeze to one.

3. **Cap, and announce it.** The limit is a runaway guard, not an editorial one:
   an 8000-char cap once sliced a judgment mid-sentence and dropped the operative
   directions. If it ever fires the text says so — a lossy document must never
   look complete.

★ Why the banner matcher is deliberately strict. The obvious rule — "drop
everything from Warning:/Notice: onwards" — is a DATA-LOSS bug in this domain:
Indian orders say "Notice: issue notice returnable on …" constantly, and "fatal
error" can appear as ordinary prose. So a marker alone never matches; the span
must also carry PHP evidence (a ``.php`` path, ``Stack trace:``, ``Uncaught``,
``{main}``) within 200 characters of it.
"""
from __future__ import annotations

import re

# A whole judgment, not a snippet — see the module docstring on capping.
MAX_ORDER_TEXT = 200_000
TRUNCATION_MARKER = "\n\n[... order text truncated ...]"

# Separator handed to BeautifulSoup.get_text() by callers, so block elements
# become line breaks rather than running together.
BLOCK_SEPARATOR = "\n"

# PHP's diagnostic prefixes as rendered into HTML (the space before the colon is
# real: PHP emits "<b>Fatal error</b> :  …" and the tag strip leaves it).
_MARKER = (
    r"(?:Fatal\s+error|Parse\s+error|Warning|Notice|Deprecated|Strict\s+Standards)\s*:"
)

# Structural proof that the marker really is a PHP banner and not prose.
_EVIDENCE = r"(?:\.php\b|Stack\s+trace\s*:|Uncaught\s|\{main\})"

# Marker + nearby evidence, then EITHER the classic "on line N" terminator
# (preferred — alternation tries it first, so a banner sitting mid-text only eats
# itself) OR the rest of the string, which is what saves us when an upstream
# length cap has already sliced the terminator off.
_BANNER_RE = re.compile(
    _MARKER + rf"(?=.{{0,200}}?{_EVIDENCE})" + r"(?:.{0,4000}?\son\s+line\s+\d+|.*\Z)",
    re.IGNORECASE | re.DOTALL,
)


def strip_server_error_banners(text: str) -> str:
    """Remove PHP notice/warning/fatal-error banners from scraped page text."""
    return _BANNER_RE.sub(" ", text)


def normalize_page_text(text: str) -> str:
    """Collapse horizontal whitespace per line; squeeze blank-line runs to one.

    Deliberately NOT ``" ".join(text.split())`` — that would erase the line
    breaks the renderer relies on to lay the order out the way the court did.
    """
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def clean_order_text(
    value: str | None,
    *,
    max_len: int = MAX_ORDER_TEXT,
    truncation_marker: str = TRUNCATION_MARKER,
) -> str | None:
    """Strip error banners, normalise whitespace, cap. None if nothing is left.

    Returns None for input that was nothing BUT a banner: an order sheet that
    failed to render carries no order, and an empty shell that reads like an
    order is worse than no order at all.

    ★ Order of operations matters. Capping BEFORE stripping leaves a truncated
    banner inside the window — unstrippable, because its terminator got cut — and
    spends the budget on text that is about to be deleted, squeezing out the real
    order.
    """
    if not value:
        return None
    text = normalize_page_text(strip_server_error_banners(str(value)))
    if not text:
        return None
    if len(text) > max_len:
        return text[:max_len] + truncation_marker
    return text
