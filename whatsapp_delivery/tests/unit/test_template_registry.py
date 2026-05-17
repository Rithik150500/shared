"""Tests for ``whatsapp_delivery.templates`` (YAML registry).

Exercises the loader against the bundled YAMLs to lock in:
  - all 21 ``nowlez_*`` filings load cleanly across 11 distinct templates;
  - the Munshi templates extracted from ``templates_filed.yml`` still parse;
  - variable positions are sequential per template language;
  - URL-button URLs are HTTPS (Meta won't approve plain HTTP);
  - bodies stay under Meta's 1024-char cap.

The test runs against the actual bundled YAMLs, not fixtures — the registry
is a small set of files we ship, so the strongest check is "the package
loads as shipped". We clear the ``lru_cache`` between tests that mutate the
registry path; the other tests share one parse.
"""
from __future__ import annotations

import pytest

from whatsapp_delivery import templates as reg
from whatsapp_delivery.errors import TemplateNotFound
from whatsapp_delivery.templates import (
    TemplateAccessor,
    get_template,
    list_templates,
)


# ---------------------------------------------------------------------------
# Round-trip: get_template
# ---------------------------------------------------------------------------


def test_get_template_by_full_name_returns_accessor():
    t = get_template("nowlez_new_order_v1", "en_US")
    assert isinstance(t, TemplateAccessor)
    assert t.full_name == "nowlez_new_order_v1"
    assert t.category == "UTILITY"


def test_get_template_hi_in_variant_uses_devanagari():
    t = get_template("nowlez_new_order_v1", "hi_IN")
    assert "आपके केस पर नया आदेश" in t.body


def test_get_template_signup_welcome_has_one_body_variable():
    t = get_template("nowlez_signup_welcome_v1", "en_US")
    body_vars = t.body_variables_in_order()
    assert [v.name for v in body_vars] == ["user_name"]


def test_get_template_new_order_has_document_header():
    t = get_template("nowlez_new_order_v1", "en_US")
    assert t.has_document_header() is True


def test_get_template_status_change_has_button_url_variable():
    """status_change has 4 body vars + 1 case_id URL var (plan §123)."""
    t = get_template("nowlez_status_change_v1", "en_US")
    button_vars = t.button_url_variables_in_order()
    assert [v.name for v in button_vars] == ["case_id"]
    assert button_vars[0].position == 5


def test_get_template_unknown_full_name_raises():
    with pytest.raises(TemplateNotFound):
        get_template("nowlez_does_not_exist_v1", "en_US")


def test_get_template_unknown_language_raises():
    with pytest.raises(TemplateNotFound):
        get_template("nowlez_test_smoke_v1", "hi_IN")  # test_smoke is en_US only


# ---------------------------------------------------------------------------
# Aggregate / invariants
# ---------------------------------------------------------------------------


def test_list_templates_includes_all_21_nowlez_filings():
    """11 distinct nowlez templates, 21 total filings (test_smoke hi_IN absent)."""
    nowlez = [t for t in list_templates() if t.full_name.startswith("nowlez_")]
    distinct = {t.full_name for t in nowlez}
    assert len(distinct) == 11

    total_filings = sum(len(t.languages) for t in nowlez)
    assert total_filings == 21


def test_list_templates_includes_munshi_extracts():
    """Munshi templates from templates_filed.yml are migrated to munshi/*.yml."""
    munshi_names = {
        t.full_name
        for t in list_templates()
        if not t.full_name.startswith("nowlez_")
    }
    # Spot-check: at least the canonical monitoring + causelist + re_engage
    # templates from templates_filed.yml are present.
    expected_present = {
        "new_order_v1",
        "hearing_changed_v1",
        "tomorrow_causelist_v1",
        "re_engage_z_v1",
        "order_judgment_v1",
    }
    assert expected_present.issubset(munshi_names)


def test_all_template_bodies_under_meta_1024_char_cap():
    """Meta enforces 1024 chars per body component."""
    for t in list_templates():
        for code, lang_spec in t.languages.items():
            assert len(lang_spec.body) <= 1024, (
                f"{t.full_name}/{code}: body is {len(lang_spec.body)} chars"
            )


def test_body_variable_positions_are_sequential_from_1():
    """Meta numbers body variables 1..N — any gap is a filing error."""
    for t in list_templates():
        for code, lang_spec in t.languages.items():
            body_vars = [v for v in lang_spec.variables if v.location == "body"]
            positions = sorted(v.position for v in body_vars)
            assert positions == list(range(1, len(positions) + 1)), (
                f"{t.full_name}/{code} body positions: {positions}"
            )


def test_button_urls_use_https_only():
    """Meta rejects plain HTTP URLs in template URL buttons at filing time."""
    for t in list_templates():
        for code, lang_spec in t.languages.items():
            for btn in lang_spec.buttons:
                if btn.type == "url":
                    assert btn.url is not None
                    assert btn.url.startswith("https://"), (
                        f"{t.full_name}/{code} button URL not HTTPS: {btn.url}"
                    )


def test_nowlez_button_urls_target_app_nowlez_in():
    """All nowlez URL buttons point at app.nowlez.in (the verified domain)."""
    for t in list_templates():
        if not t.full_name.startswith("nowlez_"):
            continue
        for code, lang_spec in t.languages.items():
            for btn in lang_spec.buttons:
                if btn.type == "url":
                    assert "app.nowlez.in" in btn.url, (
                        f"{t.full_name}/{code}: URL not on app.nowlez.in: {btn.url}"
                    )


def test_template_accessor_proxies_to_language_spec():
    """The accessor exposes language-specific body + buttons."""
    en = get_template("nowlez_stop_confirmation_v1", "en_US")
    hi = get_template("nowlez_stop_confirmation_v1", "hi_IN")
    assert en.body != hi.body
    assert en.buttons[0].url == hi.buttons[0].url  # same target URL
    # but the button text is localized
    assert en.buttons[0].text != hi.buttons[0].text


def test_loader_caches_parse_result():
    """``_load_registry`` is wrapped in lru_cache(maxsize=1)."""
    a = reg._load_registry()
    b = reg._load_registry()
    assert a is b


def test_every_button_url_variable_appears_in_yaml_url():
    """If a template declares a button_url variable, the URL must reference it."""
    for t in list_templates():
        for code, lang_spec in t.languages.items():
            url_vars = [v for v in lang_spec.variables if v.location == "button_url"]
            if not url_vars:
                continue
            # All url buttons in this language should contain the {{N}}
            # placeholder for each declared url var position.
            for v in url_vars:
                placeholder = "{{" + str(v.position) + "}}"
                matched = any(
                    btn.type == "url" and btn.url and placeholder in btn.url
                    for btn in lang_spec.buttons
                )
                assert matched, (
                    f"{t.full_name}/{code} declares button_url var "
                    f"{v.name} pos={v.position} but no URL button uses {placeholder}"
                )
