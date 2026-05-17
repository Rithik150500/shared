"""Tests for ``whatsapp_delivery.i18n.translate``.

Coverage target: lock in the fallback rules so a partially-translated
Hindi dict cannot silently drop a key, and an English-only call works
without ever touching the Hindi dict.
"""
from __future__ import annotations

import pytest

from whatsapp_delivery.i18n import translate


EN = {
    "greeting": "Hello, {name}",
    "farewell": "Goodbye, {name}",
    "no_substitution": "Static string",
}
HI = {
    # Partial: only ``greeting`` is translated.
    "greeting": "नमस्ते, {name}",
}


def test_english_path_returns_en_when_lang_none():
    """No language → English."""
    assert translate(EN, HI, "greeting", None, name="Asha") == "Hello, Asha"


def test_english_path_returns_en_when_lang_en():
    """Explicit ``en`` → English."""
    assert translate(EN, HI, "greeting", "en", name="Asha") == "Hello, Asha"


def test_hindi_path_returns_hi_when_key_translated():
    """``lang='hi'`` AND key present in Hindi dict → Hindi."""
    assert translate(EN, HI, "greeting", "hi", name="Asha") == "नमस्ते, Asha"


def test_hindi_path_falls_back_to_en_when_key_missing():
    """``lang='hi'`` but key NOT in Hindi dict → English (silent fallback)."""
    # `farewell` is in EN only; calling with lang='hi' must NOT crash.
    assert translate(EN, HI, "farewell", "hi", name="Asha") == "Goodbye, Asha"


def test_static_string_no_format_args():
    """No ``**fmt`` kwargs → return string as-is (no .format() call)."""
    # str.format() would crash on a string containing literal '{' / '}';
    # the no-args branch must skip the format call entirely.
    assert translate(EN, HI, "no_substitution", "en") == "Static string"


def test_unknown_lang_treated_as_english():
    """Anything other than ``hi`` → English (no special handling for other
    locale codes; if you want them, file the Hindi dict equivalent)."""
    assert translate(EN, HI, "greeting", "fr", name="Asha") == "Hello, Asha"


def test_empty_lang_string_treated_as_english():
    """Empty string lang → English (the docstring spells this out)."""
    assert translate(EN, HI, "greeting", "", name="Asha") == "Hello, Asha"


def test_missing_english_key_raises_keyerror():
    """A typo'd key in EN must fail loudly — that's a programmer bug we
    do NOT want to silently degrade to a blank message."""
    with pytest.raises(KeyError):
        translate(EN, HI, "this_key_does_not_exist", "en")


def test_missing_english_key_under_hindi_still_raises():
    """Even with ``lang='hi'``: if the key is missing from BOTH dicts, the
    fallback path lands on English and KeyError surfaces."""
    with pytest.raises(KeyError):
        translate(EN, HI, "unknown_key", "hi")
