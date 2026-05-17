"""Best-effort PDF row extraction from HC cause-list PDFs.

HC cause-lists are PDFs with position-based (not ruled-table) layouts. Different
benches use different column widths, font choices, and section markers. This
parser implements a robust-but-imperfect extraction strategy:

1. Find the column-header row (the line containing 'S.No.' and 'Case Number')
2. Use the x-position of 'S.No.' to identify entry-starting lines (a digit at
   that x kicks off a new case row)
3. Aggregate continuation lines into the current case until the next entry start
   or a new section marker (e.g. 'LUNCH MOTION', 'ADMISSION', 'DISPOSAL')
4. Within each case bundle, the leftmost token after the serial is the case_number

It captures every case's full text bundle in `raw_text` (always reliable) and
attempts column splitting (best-effort) for `case_number`, `parties`, `advocates`.

The full plan calls for per-bench layout heuristics (Bombay HC, Madras HC etc.).
This parser is tuned for AP HC; other benches need their own snapshot tests
before they can be considered production-ready.
"""
from __future__ import annotations

import io
import re
from typing import Any

import pdfplumber

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import HCCauseListPDFRow


# Section markers seen across HC PDFs. The set is the union over benches we've
# captured (AP HC, Tripura HC) -- adding more is safe since detection is
# substring-based (any line whose UPPER form contains a marker becomes a section).
_SECTION_MARKERS = {
    # AP HC variants
    "LUNCH MOTION", "FRESH MOTION HEARING", "FRESH MOTION", "REGULAR MOTION",
    "ADMISSION", "DISPOSAL", "FINAL HEARING", "ORDER", "FOR ORDERS",
    "FOR JUDGMENT", "ADJOURNED", "PART HEARD",
    # Tripura / Gauhati variants
    "FOR MOTION", "FOR ADMISSION", "PART I", "PART II",
}

# Column-header anchor variants used by different HCs.
_SR_NO_HEADER_RE = re.compile(
    r"^\s*(?:S\.?\s*No\.?|Sr\.?\s*No\.?|Sl\.?\s*No\.?)\s*$",
    re.IGNORECASE,
)

# Row-start markers: digit possibly followed by ')' or '.' (e.g. "1", "1)", "1.")
_ROW_START_RE = re.compile(r"^\d+[)\.]?$")


def parse_hc_cause_list_pdf(pdf_bytes: bytes) -> list[HCCauseListPDFRow]:
    """Parse a downloaded HC cause-list PDF into structured row records.

    Returns an empty list (rather than raising) if the PDF is empty or the
    column-header anchor isn't found. Raises SchemaChanged only on truly
    catastrophic input (zero-page PDF, encrypted, etc.).
    """
    # Some HC PDFs come prefixed with whitespace from PHP echo -- strip before parsing.
    pdf_bytes = pdf_bytes.lstrip()
    if not pdf_bytes.startswith(b"%PDF"):
        raise SchemaChanged(field="pdf_magic", reason="content does not start with %PDF")

    rows_out: list[HCCauseListPDFRow] = []
    sr_counter = 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            return []
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=True)
            if not words:
                continue
            sr_x, header_y = _find_sr_no_anchor(words)
            if sr_x is None or header_y is None:
                continue
            current_section, page_rows, sr_counter = _walk_page(
                words, sr_x=sr_x, header_y=header_y, starting_sr=sr_counter,
            )
            rows_out.extend(page_rows)

    return rows_out


def _find_sr_no_anchor(words: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Locate the column-header line.

    Two strategies, in order:
    1. **Explicit header** -- match "S.No.", "Sr.No.", or "Sl.No." next to "Case"/"Party".
       Used by AP HC and similar.
    2. **Implicit (first row marker)** -- find the topmost line whose leftmost word
       matches `1`, `1)`, or `1.`. Used by HCs like Tripura that omit the column
       header. We use that word's x as the anchor and y-1 as a synthetic header_y
       so subsequent processing treats this and following lines as data rows.
    """
    # Strategy 1
    for w in words:
        if _SR_NO_HEADER_RE.match(w["text"]):
            y = w["top"]
            neighbours = [
                ww["text"].lower()
                for ww in words
                if abs(ww["top"] - y) < 5 and ww["x0"] > w["x0"]
            ]
            if any("case" in n or "party" in n for n in neighbours):
                return w["x0"], y

    # Strategy 2: leftmost word of the topmost line that matches a row marker.
    # Set header_y a few px ABOVE the row so the row itself isn't excluded by
    # _walk_page's `w["top"] <= header_y + 2` filter.
    from collections import defaultdict
    by_y: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for w in words:
        y_bucket = int(round(w["top"] / 5) * 5)
        by_y[y_bucket].append(w)
    for y in sorted(by_y.keys()):
        line_words = sorted(by_y[y], key=lambda w: w["x0"])
        if not line_words:
            continue
        leftmost = line_words[0]
        if _ROW_START_RE.match(leftmost["text"]):
            return leftmost["x0"], leftmost["top"] - 10
    return None, None


def _walk_page(
    words: list[dict[str, Any]],
    *,
    sr_x: float,
    header_y: float,
    starting_sr: int,
) -> tuple[str, list[HCCauseListPDFRow], int]:
    """Sweep the page top-to-bottom, grouping words into rows by y-band, then
    aggregate consecutive rows into case bundles.
    """
    # Bucket all words by y (5px buckets) -- we need section markers from above
    # header_y too, since some HCs (Tripura) put 'FOR MOTION' between the title
    # and the first row.
    from collections import defaultdict
    full_by_y: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for w in words:
        y_bucket = int(round(w["top"] / 5) * 5)
        full_by_y[y_bucket].append(w)

    # `by_y` is the row-data view (excludes everything at or above header_y).
    by_y: dict[int, list[dict[str, Any]]] = {
        y: ws for y, ws in full_by_y.items() if y > header_y + 2
    }

    sorted_ys = sorted(full_by_y.keys())  # iterate the FULL page so section markers fire
    current_section = "DEFAULT"
    bundles: list[dict[str, Any]] = []
    current_bundle: dict[str, Any] | None = None
    sr_counter = starting_sr
    # Some HC PDFs have an "advocates on leave" trailer with re-numbered 1)-N)
    # entries that aren't case rows. Once we see the marker phrase we stop adding
    # bundles entirely.
    in_trailer = False

    for y in sorted_ys:
        line_words = sorted(full_by_y[y], key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in line_words).strip()
        if not line_text:
            continue

        upper = line_text.upper()
        if (
            "COUNSEL NAMED BELOW" in upper
            or "ADVOCATES ON LEAVE" in upper
            or "REMAIN ABSENT" in upper
        ):
            in_trailer = True
            continue
        if in_trailer:
            continue

        # Section marker? Track the section regardless of where it appears on the page.
        for marker in _SECTION_MARKERS:
            if marker in upper:
                current_section = marker
                line_text = ""
                break
        if not line_text:
            continue

        # Below this point we're processing data lines. If we're still above
        # the row-data region, skip.
        if y not in by_y:
            continue

        # Does this line START a new case (row-marker token at sr_x position)?
        first = line_words[0]
        if _ROW_START_RE.match(first["text"]) and abs(first["x0"] - sr_x) < 12:
            sr_counter += 1
            current_bundle = {
                "sr_no": sr_counter,
                "section": current_section,
                "lines": [line_text],
                "words": list(line_words),
            }
            bundles.append(current_bundle)
        else:
            if current_bundle is not None:
                current_bundle["lines"].append(line_text)
                current_bundle["words"].extend(line_words)

    rows: list[HCCauseListPDFRow] = []
    for b in bundles:
        rows.append(_bundle_to_row(b))

    return current_section, rows, sr_counter


def _bundle_to_row(bundle: dict[str, Any]) -> HCCauseListPDFRow:
    """Convert a bundle of grouped words into a HCCauseListPDFRow.

    Best-effort column extraction:
    - case_number: leftmost non-digit token of the FIRST line (after sr_no)
    - raw_text: every line joined with newlines (reliable, ground truth)
    - parties / advocates: split by x-position when possible, else empty
    """
    raw_text = "\n".join(bundle["lines"])

    # case_number: first non-row-marker token on line 1
    first_line_words = sorted(
        [w for w in bundle["words"] if w in bundle["words"][: max(1, len(bundle["lines"][0].split()) + 2)]],
        key=lambda w: w["x0"],
    )
    case_number = ""
    saw_marker = False
    for w in first_line_words:
        if not saw_marker and _ROW_START_RE.match(w["text"]):
            saw_marker = True
            continue
        if saw_marker:
            case_number = w["text"]
            break

    return HCCauseListPDFRow(
        sr_no=bundle["sr_no"],
        section=bundle["section"],
        case_number=case_number,
        raw_text=raw_text,
        parties="",       # left empty -- structured column split is unreliable across benches
        advocates="",
    )
