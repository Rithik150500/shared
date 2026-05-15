"""Password strength validation.

Two checks for now: minimum length (8), and presence in a small common-passwords
deny list. Extend with entropy / class-mix rules later if needed.
"""
from __future__ import annotations

from pathlib import Path

from ..errors import PasswordTooWeak

_COMMON_PWS: set[str] = set()


def _load_common_passwords() -> None:
    if _COMMON_PWS:
        return
    f = Path(__file__).parent / "common_passwords.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line:
                _COMMON_PWS.add(line)


def validate_password_strength(plain: str) -> None:
    """Raise PasswordTooWeak if the password fails any rule."""
    _load_common_passwords()
    if len(plain) < 8:
        raise PasswordTooWeak("Password must be at least 8 characters")
    if plain.lower() in _COMMON_PWS:
        raise PasswordTooWeak("Password is too common")
