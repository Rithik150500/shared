"""Tiny copy-localization helper.

Each feature module's `copy.py` exposes:

    COPY:    dict[str, str]    # English (canonical)
    COPY_HI: dict[str, str]    # Hindi (subset; missing keys fall back to English)

and a thin convenience function `t(key, lang, **fmt)` that calls into this
module's `translate()`. This split keeps the fallback logic in one place while
letting each module own its own dictionaries (so a missing key surfaces as a
KeyError pointing at the right module).

Design notes
------------
- We intentionally keep the API tiny (one function, four positional args). Any
  fancier i18n needs (pluralization, ICU formats, RTL) live downstream.
- The fallback rule is: if `lang == 'hi'` AND key exists in COPY_HI, use it.
  Otherwise English. This means **partially-translated dicts are safe**: missing
  Hindi keys silently degrade to English instead of crashing.
- KeyError on the English dict is loud on purpose -- that's a programmer bug
  (typo'd key) and we want it to fail in tests, not silently send a blank
  message to a user.
"""
from __future__ import annotations

from typing import Mapping


def translate(
    en: Mapping[str, str],
    hi: Mapping[str, str],
    key: str,
    lang: str | None,
    **fmt: object,
) -> str:
    """Pick the localized string for ``key``/``lang`` and apply ``fmt``.

    Params
    ------
    en : the English (canonical) dictionary -- must contain ``key`` or KeyError
    hi : the Hindi dictionary -- may be partial; missing keys fall back to en
    key : the copy key (e.g. "save_already")
    lang : the user's language preference -- "hi" for Hindi, anything else
        (None, "en", "" ) is treated as English.
    fmt : optional named substitutions for ``str.format`` -- e.g.
        ``translate(en, hi, "save_already", "hi", label="Smith vs Bank")``.
    """
    table: Mapping[str, str]
    if lang == "hi" and key in hi:
        table = hi
    else:
        table = en
    text = table[key]
    return text.format(**fmt) if fmt else text
