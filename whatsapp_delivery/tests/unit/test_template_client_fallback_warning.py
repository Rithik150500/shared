"""D-9 audit fix: ``META_TEMPLATES_FALLBACK_TO_TEXT`` must not be silent.

Previously, the three ``send_template*`` methods each checked the env var
inline and silently degraded to ``send_text`` if it was set. Operators only
noticed in production when Meta 24h-window errors started rolling in --
because dev/staging had leaked the flag.

These tests pin the post-fix contract:

  * The first fallback in a process emits a WARNING log.
  * Subsequent fallbacks do NOT re-emit the warning (otherwise we'd flood
    logs at production-template volume).
  * Every fallback bumps an in-process counter so monitoring can alert on
    ``fallback_to_text_total > 0`` regardless of the once-per-process log.
"""
from __future__ import annotations

import json
import logging

import pytest
import respx
from httpx import Response

from whatsapp_delivery import template_client as tc_module
from whatsapp_delivery.template_client import TemplateClient


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _make_client() -> TemplateClient:
    return TemplateClient(phone_number_id="111", access_token="tok")


@pytest.fixture(autouse=True)
def _reset_fallback_module_state():
    """The warning flag and metric counter are module-level globals -- reset
    them between tests so order-of-execution can't influence pass/fail."""
    tc_module._FALLBACK_TO_TEXT_WARNED = False
    tc_module._METRICS["fallback_to_text_total"] = 0
    yield
    tc_module._FALLBACK_TO_TEXT_WARNED = False
    tc_module._METRICS["fallback_to_text_total"] = 0


@respx.mock
def test_first_fallback_logs_warning_and_increments_counter(monkeypatch, caplog):
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.first"}]})
    )

    with caplog.at_level(logging.WARNING, logger="whatsapp_delivery.template_client"):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=["Alice"],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING on first fallback; got {len(warnings)}"
    )
    assert "META_TEMPLATES_FALLBACK_TO_TEXT" in warnings[0].getMessage()
    assert tc_module._METRICS["fallback_to_text_total"] == 1


@respx.mock
def test_second_fallback_increments_counter_without_relogging(monkeypatch, caplog):
    """Counter keeps going; warning does not repeat."""
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.x"}]})
    )

    client = _make_client()
    # First call drains the warning.
    client.send_template(
        to="+919999999999", name="welcome_v1", language="en_US", variables=["A"],
    )

    # Now record the second call.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="whatsapp_delivery.template_client"):
        client.send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=["B"],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0, (
        f"second fallback must not re-emit the warning; got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]}"
    )
    assert tc_module._METRICS["fallback_to_text_total"] == 2


@respx.mock
def test_fallback_metric_increments_across_send_template_variants(monkeypatch):
    """All three send_template* helpers share the counter (and the warning)."""
    monkeypatch.setenv("META_TEMPLATES_FALLBACK_TO_TEXT", "1")
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.q"}]})
    )

    client = _make_client()
    client.send_template(
        to="+91", name="a", language="en_US", variables=[],
    )
    client.send_template_with_document(
        to="+91", name="b", language="en_US", variables=[], document_media_id="m",
    )
    client.send_template_with_components(
        to="+91", name="c", language="en_US", body_variables=[],
    )

    assert tc_module._METRICS["fallback_to_text_total"] == 3


@respx.mock
def test_env_unset_emits_no_warning_and_no_metric(monkeypatch, caplog):
    """If the env var is not "1", no warning, no metric -- baseline."""
    monkeypatch.delenv("META_TEMPLATES_FALLBACK_TO_TEXT", raising=False)
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.n"}]})
    )

    with caplog.at_level(logging.WARNING, logger="whatsapp_delivery.template_client"):
        _make_client().send_template(
            to="+919999999999", name="welcome_v1", language="en_US", variables=[],
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []
    assert tc_module._METRICS["fallback_to_text_total"] == 0
