"""Order-text sanitizer: strip server-side error banners the portal renders
into the page body.

The banner below is the REAL one captured from tdsat.gov.in on 2026-08-09
(whitespace already collapsed, exactly as the extractor sees it). TDSAT's
orderp.php crashes in dompdf AFTER emitting the order, so every TDSAT order
sheet ends with it. It reached users: casepilot renders order_text into a PDF,
and 17 such PDFs were written before this was caught.
"""
from __future__ import annotations

from ecourts_client._order_text import clean_order_text

_REAL_TDSAT_BANNER = (
    "Fatal error : Uncaught Error: Call to undefined function "
    "Dompdf\\mb_internal_encoding() in "
    "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php:313 Stack trace: "
    "#0 /home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php(287): "
    "Dompdf\\Dompdf->setPhpConfig() #1 "
    "/home/www/html/tdsat/Delhi/services/orderp.php(17): "
    "Dompdf\\Dompdf->__construct() #2 {main} thrown in "
    "/home/www/html/tdsat/vendor/dompdf/dompdf/src/Dompdf.php on line 313"
)

_REAL_ORDER = (
    "TELECOM DISPUTES SETTLEMENT & APPELLATE TRIBUNAL NEW DELHI Dated: 25/05/2026 "
    "BROADCASTING PETITION/345/2024 BEFORE MR. SHASHI KANT SHARMA ,DEPUTY REGISTRAR "
    "ORDER At the request by the Counsel for the Respondents, these matters are "
    "adjourned to 13.7.2026 for framing of Issues. ( SHASHI KANT SHARMA) DEPUTY REGISTRAR"
)


def test_strips_the_real_tdsat_fatal_error_banner():
    out = clean_order_text(f"{_REAL_ORDER} {_REAL_TDSAT_BANNER}")
    assert out == _REAL_ORDER
    # the tail fragment users actually saw in the rendered PDF
    assert "Dompdf.php" not in out
    assert "thrown in" not in out
    assert "Stack trace" not in out


def test_order_body_survives_intact():
    out = clean_order_text(f"{_REAL_ORDER} {_REAL_TDSAT_BANNER}")
    assert out.endswith("DEPUTY REGISTRAR")
    assert "adjourned to 13.7.2026 for framing of Issues." in out


def test_strips_a_truncated_banner_with_no_on_line_terminator():
    """An upstream cap can cut the banner before `on line N`. Stripping must not
    depend on the terminator being present, or a half-banner survives."""
    truncated = (
        "Fatal error : Uncaught Error: Call to undefined function "
        "Dompdf\\mb_internal_encoding() in /home/www/html/tdsat/vendor/dompdf/dom"
    )
    out = clean_order_text(f"{_REAL_ORDER} {truncated}")
    assert out == _REAL_ORDER


def test_strips_warning_and_deprecated_and_parse_error_banners():
    for banner in (
        "Warning : include(): Failed opening 'x.php' for inclusion in /var/www/a.php on line 22",
        "Deprecated : Function ereg() is deprecated in /var/www/legacy/b.php on line 7",
        "Parse error : syntax error, unexpected ';' in /srv/app/c.php on line 91",
        "Notice : Undefined index: caseno in /srv/app/d.php on line 4",
    ):
        out = clean_order_text(f"{_REAL_ORDER} {banner}")
        assert out == _REAL_ORDER, banner


def test_banner_at_the_start_is_stripped_too():
    out = clean_order_text(f"{_REAL_TDSAT_BANNER} {_REAL_ORDER}")
    assert out == _REAL_ORDER


# --- false-positive guards: court orders legitimately use these words --------

def test_does_not_touch_a_court_notice():
    """'Notice' is everywhere in Indian orders. Stripping on the bare word would
    silently delete the operative part of the order."""
    order = (
        "ORDER Notice: issue notice to the respondents returnable on 12.09.2026. "
        "Warning: the respondent is cautioned against further delay."
    )
    assert clean_order_text(order) == order


def test_does_not_strip_a_marker_without_a_php_path():
    order = "ORDER Fatal error was alleged by the complainant on line 4 of the report."
    assert clean_order_text(order) == order


def test_does_not_strip_a_php_mention_without_an_error_marker():
    order = "ORDER The portal page order.php was produced in evidence on line 3."
    assert clean_order_text(order) == order


# --- shape contract ---------------------------------------------------------

def test_collapses_horizontal_whitespace_but_keeps_line_breaks():
    """Line structure is the court's own layout — these pages are tables, one
    <td> per line — so it must survive; only horizontal runs collapse."""
    out = clean_order_text("  ORDER\n\tallowed   in   part \n\n\n signed \n")
    assert out.splitlines() == ["ORDER", "allowed in part", "", "signed"]


def test_banner_only_input_yields_none():
    assert clean_order_text(_REAL_TDSAT_BANNER) is None


def test_empty_and_none_yield_none():
    assert clean_order_text("") is None
    assert clean_order_text(None) is None
    assert clean_order_text("    ") is None


def test_cap_is_applied_after_stripping_not_before():
    """Capping first would leave the banner's tail inside the cap window and cut
    the real order short — the exact shape of the bug being fixed."""
    from ecourts_client._order_text import TRUNCATION_MARKER

    body = "ORDER " + ("x" * 400)
    out = clean_order_text(f"{body} {_REAL_TDSAT_BANNER}", max_len=50)
    assert out == body[:50] + TRUNCATION_MARKER
    assert "Dompdf" not in out
