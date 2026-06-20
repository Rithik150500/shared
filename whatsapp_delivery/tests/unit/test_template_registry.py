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
    t = get_template("nowlez_signup_welcome_v2", "en_US")
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


def test_list_templates_includes_all_35_nowlez_filings():
    """18 distinct nowlez templates, 35 total filings.

    Original 11 (10 × {en_US,hi} + test_smoke en_US-only = 21 filings) + the 7
    billing-lifecycle templates added in billing.yml (each × {en_US,hi} = 14
    filings) → 18 distinct, 35 filings.
    """
    nowlez = [t for t in list_templates() if t.full_name.startswith("nowlez_")]
    distinct = {t.full_name for t in nowlez}
    assert len(distinct) == 18

    total_filings = sum(len(t.languages) for t in nowlez)
    assert total_filings == 35


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


def test_nowlez_button_urls_target_www_nowlez_com():
    """All nowlez URL buttons point at www.nowlez.com (the live web origin).

    app.nowlez.in was the original (dead) domain; templates were migrated to
    www.nowlez.com 2026-06-20 in both the live Meta WABA and this registry.
    """
    for t in list_templates():
        if not t.full_name.startswith("nowlez_"):
            continue
        for code, lang_spec in t.languages.items():
            for btn in lang_spec.buttons:
                if btn.type == "url":
                    assert "www.nowlez.com" in btn.url, (
                        f"{t.full_name}/{code}: URL not on www.nowlez.com: {btn.url}"
                    )


def test_template_accessor_proxies_to_language_spec():
    """The accessor exposes language-specific body + buttons."""
    en = get_template("nowlez_stop_confirmation_v2", "en_US")
    hi = get_template("nowlez_stop_confirmation_v2", "hi_IN")
    assert en.body != hi.body
    assert en.buttons[0].url == hi.buttons[0].url  # same target URL
    # but the button text is localized
    assert en.buttons[0].text != hi.buttons[0].text


def test_loader_caches_parse_result():
    """``_load_registry`` returns the same dict on repeated calls.

    D-8: the cache was migrated from ``functools.lru_cache(maxsize=1)``
    to a module-level dict + sentinel so the cache is exception-safe and
    invalidatable, but the surface contract (cheap repeat calls) is
    preserved.
    """
    reg.invalidate_registry_cache()  # Ensure a clean slate.
    a = reg._load_registry()
    b = reg._load_registry()
    assert a is b


def test_invalidate_registry_cache_forces_reparse():
    """After invalidate, the next call returns a fresh dict object.

    D-8: tests / operators can force the YAML files to be re-read
    without process restart. The new dict has the same contents but a
    different identity than the pre-invalidate dict.
    """
    a = reg._load_registry()
    reg.invalidate_registry_cache()
    b = reg._load_registry()
    assert a is not b, "invalidate should force a fresh parse"
    # ...but the parsed contents are equal (same on-disk YAML).
    assert set(a.keys()) == set(b.keys())


def test_failed_parse_does_not_poison_cache(monkeypatch):
    """D-8 regression test: a bad-YAML failure must NOT permanently break
    every subsequent ``_load_registry`` call.

    The old ``functools.lru_cache`` cached the exception object, so once
    the registry raised it kept raising on every subsequent call until
    process restart. The new module-level cache leaves ``_REGISTRY`` at
    None on failure so the next call retries the file read.
    """
    reg.invalidate_registry_cache()

    # First call: raise mid-parse.
    bad_marker = []
    real_parse = reg._parse_entry

    def boom(entry):
        if not bad_marker:
            bad_marker.append(1)
            raise ValueError("simulated bad YAML")
        return real_parse(entry)

    monkeypatch.setattr(reg, "_parse_entry", boom)
    with pytest.raises(ValueError, match="simulated bad YAML"):
        reg._load_registry()
    assert reg._REGISTRY is None, (
        "failed parse must NOT populate _REGISTRY — that's what enables "
        "retry on the next call"
    )

    # Second call after the simulated transient resolves: must parse
    # cleanly, no cached exception.
    monkeypatch.setattr(reg, "_parse_entry", real_parse)
    result = reg._load_registry()
    assert isinstance(result, dict)
    assert len(result) > 0


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
