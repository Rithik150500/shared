"""HMAC-SHA256 verification for Meta's X-Hub-Signature-256 header."""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header. Constant-time compare."""
    if header is None or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header[len("sha256="):]
    return hmac.compare_digest(expected, provided)
