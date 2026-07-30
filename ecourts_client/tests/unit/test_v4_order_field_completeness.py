"""``encode_v4_order`` must not silently blank missing fields.

Prod 2026-07-30: ``display_pdf_new.php`` answers HTTP 200 with a 14-byte body
``'Invalid Input4'`` for a subset of orders -- every observed instance on Delhi
High Court. That is NOT the burst-throttle page (documented at ~228 bytes,
"Welcome User Search Page not Found here", RE_NOTES_v4.md:96) and it is NOT the
"no document" answer either -- eCourts says that verbosely, as
``"order is not uploaded for - case no- CW/0024406/2025"``. A terse indexed
error is a parameter-validation rejection.

The mechanism that would produce one::

    _V4_ORDER_FIELDS = ("filename", "caseno", "cCode", "appFlag",
                        "state_cd", "dist_cd", "court_code")
    urlencode({k: str(row.get(k, "")) for k in _V4_ORDER_FIELDS})   # pdf.py:41

``row.get(k, "")`` turns any absent field into an empty string, and the only
upstream guard checks ``filename`` and ``order_date``
(parsers/case_history.py:214-216). High Court and District share one parser
(highcourt.py:72, district.py:102), so an HC row shaped differently from the
district row the field list was derived from yields a request with a blank in
that slot -- and no signal anywhere that it happened.

These tests do not change behaviour. The URL is still produced exactly as
before, because we do not yet know which fields eCourts actually requires and
guessing would break orders that currently work. They add the ONE thing that
turns this from a hypothesis into an answer: a log line naming the missing
field, so the next prod poll tells us whether "Input4" really is ``appFlag``.
"""
from __future__ import annotations

import pytest

from ecourts_client.pdf import _V4_ORDER_FIELDS, encode_v4_order


def _complete_row(**over):
    row = {
        "filename": "order_1.pdf",
        "caseno": "CW/0024406/2025",
        "cCode": "1",
        "appFlag": "1",
        "state_cd": "7",
        "dist_cd": "1",
        "court_code": "1",
    }
    row.update(over)
    return row


def test_complete_row_encodes_all_seven_fields_and_logs_nothing(caplog):
    with caplog.at_level("WARNING"):
        url = encode_v4_order(_complete_row())
    for field in _V4_ORDER_FIELDS:
        assert f"{field}=" in url
    assert "ECOURTS_ORDER_FIELDS_MISSING" not in caplog.text


def test_missing_field_is_named_in_a_log_line(caplog):
    """The diagnostic that settles 'Invalid Input4'. Without this, a blank
    parameter is indistinguishable from a populated one at every layer we own."""
    row = _complete_row()
    del row["appFlag"]
    with caplog.at_level("WARNING"):
        encode_v4_order(row)
    assert "ECOURTS_ORDER_FIELDS_MISSING" in caplog.text
    assert "appFlag" in caplog.text


def test_empty_string_counts_as_missing(caplog):
    """What reaches eCourts is an empty value either way, so a present-but-blank
    field must be reported identically to an absent one."""
    with caplog.at_level("WARNING"):
        encode_v4_order(_complete_row(court_code=""))
    assert "ECOURTS_ORDER_FIELDS_MISSING" in caplog.text
    assert "court_code" in caplog.text


def test_all_missing_fields_are_reported_together(caplog):
    row = {"filename": "x.pdf", "caseno": "CW/1/2025"}
    with caplog.at_level("WARNING"):
        encode_v4_order(row)
    text = caplog.text
    for field in ("cCode", "appFlag", "state_cd", "dist_cd", "court_code"):
        assert field in text, f"{field} not reported"


def test_behaviour_is_unchanged(caplog):
    """THE SAFETY PROPERTY. This commit is diagnostic only: the URL for an
    incomplete row must be byte-identical to what shipped before, or we would be
    breaking orders that currently work in order to investigate ones that do
    not."""
    row = _complete_row()
    del row["appFlag"]
    with caplog.at_level("WARNING"):
        url = encode_v4_order(row)
    assert url.startswith("displaypdf:")
    assert "appFlag=" in url, "the blank field must still be sent, exactly as before"
    # and the order of fields is still the declared order
    idx = [url.index(f"{f}=") for f in _V4_ORDER_FIELDS]
    assert idx == sorted(idx)
