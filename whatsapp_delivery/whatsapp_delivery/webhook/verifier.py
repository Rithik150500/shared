"""HMAC-SHA256 verification for Meta's X-Hub-Signature-256 header.

D-2: Two additions beyond the naive verify:

* ``validate_secret`` -- callable at startup (or imported and exercised at
  module load by the consumer) -- raises ``ValueError`` if the configured
  ``META_APP_SECRET`` is contaminated by a BOM, surrounding whitespace, or
  is empty. The motivation: a single stray newline at the end of a
  ``.env`` value makes every signature silently fail-closed, and the
  operator sees "webhooks 403" with no idea whether the secret is wrong
  or whether Meta is the problem. Failing loud at startup catches it
  before traffic flows.

* The verify function logs a WARNING when the header is missing/empty so
  ops can distinguish "Meta didn't sign this request" from "signature
  didn't match" -- both look like 403 to the upstream caller, but only
  one of them is a config problem.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

log = logging.getLogger(__name__)


# UTF-8 BOM, as it would appear if a .env file was saved with Notepad.
_BOM = "﻿"


def validate_secret(secret: str) -> None:
    """Fail loudly if META_APP_SECRET is contaminated.

    Raises ``ValueError`` for the three failure modes we've actually seen
    in production:

    * empty string (env not exported or value is ``""``);
    * leading or trailing whitespace (a copy-paste from a chat client or
      a trailing newline in a ``.env`` file);
    * a UTF-8 BOM at the start (Notepad / some Windows editors).

    Intended to be called from the application's startup path. Cheap
    enough that callers may invoke it at module-import time too.
    """
    if secret is None or secret == "":
        raise ValueError("META_APP_SECRET is empty; webhook verification will fail-closed")
    if secret.startswith(_BOM):
        raise ValueError(
            "META_APP_SECRET starts with a UTF-8 BOM; remove the BOM from the .env "
            "(Notepad saves files this way by default)"
        )
    if secret != secret.strip():
        raise ValueError(
            "META_APP_SECRET has leading/trailing whitespace; strip it from the .env "
            "(a trailing newline is the most common cause)"
        )


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header. Constant-time compare.

    Returns ``False`` for missing/empty/malformed headers AND for genuine
    HMAC mismatches. The two cases are distinguished only in the log
    record: a WARNING is emitted when the header is missing or empty so
    operators can tell config errors from signature-mismatch attacks.
    """
    if header is None or header == "":
        log.warning(
            "verify_signature: X-Hub-Signature-256 header missing or empty; "
            "rejecting request"
        )
        return False
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header[len("sha256="):]
    return hmac.compare_digest(expected, provided)
