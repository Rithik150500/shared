"""AES-128-CBC envelope crypto for the eCourts mobile API.

Mirrors the JS in `assets/www/js/main.js` of the v3.0 APK:
- Request encryption (`encryptData`) uses key 4D62...397A and a 16-byte IV split as
  globaliv (8 bytes from a fixed pool) || randomiv (8 random bytes). Wire format
  is `randomiv_hex(16) || globalIndex_digit(1) || base64(ciphertext)`.
- Response decryption (`decodeResponse`) uses key 3273...4B62 and an IV that is
  the first 32 hex chars (16 bytes) of the response, with base64 ciphertext
  appended.
- The Bearer header on every non-bootstrap call is `wrap_bearer(jwt)`, which
  is just `encrypt_request(jwt_string)` -- i.e. the JWT is wrapped in the same
  request envelope before being placed in `Authorization: Bearer ...`.

See docs/RE_NOTES.md section 2 for the full provenance.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


REQUEST_KEY: bytes = bytes.fromhex("4D6251655468576D5A7134743677397A")
RESPONSE_KEY: bytes = bytes.fromhex("3273357638782F413F4428472B4B6250")

GLOBAL_IV_POOL: tuple[bytes, ...] = tuple(
    bytes.fromhex(h)
    for h in (
        "556A586E32723575",
        "34743777217A2543",
        "413F4428472B4B62",
        "48404D635166546A",
        "614E645267556B58",
        "655368566D597133",
    )
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x19]+")


def _pick_global_iv() -> tuple[bytes, int]:
    index = secrets.randbelow(len(GLOBAL_IV_POOL))
    return GLOBAL_IV_POOL[index], index


def encrypt_request(payload: Any) -> str:
    """Encrypt a JSON-serializable payload into the eCourts request envelope.

    Wire format: randomiv_hex(16) || globalIndex(1) || base64(ciphertext).
    """
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    global_iv, global_index = _pick_global_iv()
    random_iv = secrets.token_bytes(8)
    iv = global_iv + random_iv

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(REQUEST_KEY), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return random_iv.hex() + str(global_index) + base64.b64encode(ciphertext).decode("ascii")


def decrypt_response(envelope: str) -> str:
    """Decrypt a response envelope and return the JSON string.

    Wire format: iv_hex(32) || base64(ciphertext).
    Strips control chars (matching the JS regex /[\\u0000-\\u0019]+/g) before returning.
    """
    s = envelope.strip()
    iv_hex, b64_ct = s[:32], s[32:]
    iv = bytes.fromhex(iv_hex)
    ciphertext = base64.b64decode(b64_ct)

    cipher = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    text = plaintext.decode("utf-8", errors="replace")
    return _CONTROL_CHARS_RE.sub("", text)


def wrap_bearer(jwt: str) -> str:
    """Wrap a JWT for use in the `Authorization: Bearer <...>` header.

    The eCourts API expects the JWT to be itself encrypted with the request envelope
    before being sent in the Bearer slot.
    """
    return encrypt_request(jwt)
