"""Unit tests for the verify_google_id_token wrapper (offline).

The google-auth network/crypto boundary (verify_oauth2_token) is patched, so
these tests exercise ONLY the wrapper's guards + exception mapping, never the
network.
"""
from unittest.mock import patch

import pytest

from identity.errors import GoogleTokenInvalid
from identity.google.verifier import verify_google_id_token


def test_empty_token_rejected():
    with pytest.raises(GoogleTokenInvalid):
        verify_google_id_token("", audience="web-client-123")


def test_empty_audience_rejected():
    with pytest.raises(GoogleTokenInvalid):
        verify_google_id_token("some-token", audience="")


def test_verify_failure_maps_to_google_token_invalid():
    # google-auth raises ValueError on any verification failure.
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("bad aud")):
        with pytest.raises(GoogleTokenInvalid):
            verify_google_id_token("header.payload.sig", audience="web-client-123")


def test_transport_error_maps_to_google_token_invalid():
    # A cert-fetch / transport error must also collapse to GoogleTokenInvalid.
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=RuntimeError("network down")):
        with pytest.raises(GoogleTokenInvalid):
            verify_google_id_token("header.payload.sig", audience="web-client-123")


def test_valid_token_returns_claims():
    claims = {"sub": "123", "email": "u@example.com", "email_verified": True}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
        out = verify_google_id_token("header.payload.sig", audience="web-client-123")
    assert out == claims
