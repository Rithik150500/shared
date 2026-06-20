"""Canonical phone-number normalization.

A single source of truth for the form a phone number takes in the shared
``users`` table (and the OTP / login-request tables that key off it). The
WhatsApp webhook already produces E.164 (``+91...``); the web / OTP provisioning
path historically stored bare 10-digit strings. Because ``UNIQUE(phone)`` treats
``9953652710`` and ``+919953652710`` as different keys, a user provisioned on the
web got a *second*, empty user row the first time they messaged the bot — the
"portfolio is empty" identity split. Funnelling every write and lookup through
``normalize_phone`` makes the two paths converge.

Default country is India (``cc="91"``): the product's user base is Indian
lawyers and the only ambiguous form is a bare 10-digit national number. A number
that already carries an explicit ``+<cc>`` is preserved as-is.
"""
from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str | None, *, default_cc: str = "91") -> str | None:
    """Return the canonical E.164-style string (``+<cc><number>``) for ``raw``.

    Rules:
      * ``None`` / blank → ``None``.
      * Already ``+``-prefixed → keep its country code, strip formatting.
      * Bare national number (10 digits, optional leading trunk ``0``) → prepend
        ``+<default_cc>``.
      * Digits-only with a country code already baked in (11+ digits, no trunk
        zero) → just prepend ``+``.

    Idempotent: ``normalize_phone(normalize_phone(x)) == normalize_phone(x)``.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None

    had_plus = s.startswith("+")
    digits = _NON_DIGITS.sub("", s)
    if not digits:
        return None

    if had_plus:
        # Country code already explicit; never re-add default_cc.
        return "+" + digits

    # International-access prefix "00" is equivalent to "+" (common in pasted /
    # dialer-formatted Indian numbers, e.g. "0091 99536 52710").
    if digits.startswith("00"):
        rest = digits[2:]
        return "+" + rest if rest else None

    # Strip a single national-trunk leading zero (e.g. Indian STD "0XXXXXXXXXX").
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10:
        return "+" + default_cc + digits

    # 11+ digits, no plus, not trunk-zero → assume the country code is present.
    return "+" + digits
