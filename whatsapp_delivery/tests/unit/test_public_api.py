"""Smoke test for the public API surface.

Locks in the names exported from ``whatsapp_delivery`` (the canonical import
path) so an accidental rename or removal during refactor fails immediately.
Also asserts each name resolves to the expected kind of object (callable
vs class vs dataclass) — protects against an export pointing at the wrong
symbol (e.g. a string constant when a function is expected).

A parallel block exercises ``whatsapp_delivery.public`` to confirm both
paths expose the same surface.
"""
from __future__ import annotations

import importlib
import inspect

import pytest


EXPECTED_NAMES = {
    # config
    "WhatsAppConfig",
    # dispatch
    "enqueue_send_document",
    "enqueue_send_template",
    "enqueue_send_template_with_components",
    "enqueue_send_text",
    "process_send_queue",
    # errors
    "Meta24HourWindowExpired",
    "MetaInvalidMessage",
    "MetaTransientError",
    "RateLimitExceeded",
    "ScaffoldError",
    "StopRequested",
    "TemplateNotFound",
    "WebhookSignatureInvalid",
    # idempotency
    "claim_message",
    # meta clients
    "MetaClient",
    "TemplateClient",
    # models
    "RouteTarget",
    # templates
    "Template",
    "TemplateAccessor",
    "get_template",
    "list_templates",
    # webhook
    "DeliveryStatus",
    "IncomingButton",
    "IncomingMedia",
    "IncomingMessage",
    "handle_stop_keyword",
    "is_stop_keyword",
    "parse_incoming",
    "parse_status_updates",
    "route_inbound",
    "verify_signature",
}


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("whatsapp_delivery")


@pytest.fixture(scope="module")
def pkg_public():
    return importlib.import_module("whatsapp_delivery.public")


def test_top_level_all_matches_expected(pkg):
    assert set(pkg.__all__) == EXPECTED_NAMES


def test_top_level_size_meets_plan_floor(pkg):
    """Plan §8.2 expects ``>= 30`` re-exports."""
    assert len(pkg.__all__) >= 30


def test_each_top_level_name_is_importable(pkg):
    for name in pkg.__all__:
        assert hasattr(pkg, name), f"missing top-level export: {name}"
        obj = getattr(pkg, name)
        assert obj is not None


# ---------------------------------------------------------------------------
# Per-name shape assertions (classes vs callables)
# ---------------------------------------------------------------------------


_CLASSES = {
    "DeliveryStatus",
    "IncomingButton",
    "IncomingMedia",
    "IncomingMessage",
    "Meta24HourWindowExpired",
    "MetaClient",
    "MetaInvalidMessage",
    "MetaTransientError",
    "RateLimitExceeded",
    "RouteTarget",
    "ScaffoldError",
    "StopRequested",
    "Template",
    "TemplateAccessor",
    "TemplateClient",
    "TemplateNotFound",
    "WebhookSignatureInvalid",
    "WhatsAppConfig",
}

_FUNCTIONS = {
    "claim_message",
    "enqueue_send_document",
    "enqueue_send_template",
    "enqueue_send_template_with_components",
    "enqueue_send_text",
    "get_template",
    "handle_stop_keyword",
    "is_stop_keyword",
    "list_templates",
    "parse_incoming",
    "parse_status_updates",
    "process_send_queue",
    "route_inbound",
    "verify_signature",
}


def test_classes_are_actual_classes(pkg):
    for name in _CLASSES:
        obj = getattr(pkg, name)
        assert inspect.isclass(obj), f"{name} should be a class, got {type(obj)!r}"


def test_functions_are_actual_callables(pkg):
    for name in _FUNCTIONS:
        obj = getattr(pkg, name)
        assert callable(obj), f"{name} should be callable, got {type(obj)!r}"


def test_partition_covers_full_export_set():
    """Every name in __all__ is either a class or a function (no constants)."""
    assert _CLASSES | _FUNCTIONS == EXPECTED_NAMES


# ---------------------------------------------------------------------------
# public.py mirrors __init__
# ---------------------------------------------------------------------------


def test_public_module_has_same_surface(pkg, pkg_public):
    assert set(pkg.__all__) == set(pkg_public.__all__)
    # And each symbol points at the same object via both paths.
    for name in pkg.__all__:
        assert getattr(pkg, name) is getattr(pkg_public, name), (
            f"{name} differs between whatsapp_delivery and whatsapp_delivery.public"
        )


# ---------------------------------------------------------------------------
# Spot-checks per the task brief
# ---------------------------------------------------------------------------


def test_get_template_returns_accessor_via_public_api(pkg):
    t = pkg.get_template("nowlez_signup_welcome_v1", "en_US")
    assert isinstance(t, pkg.TemplateAccessor)
    assert t.full_name == "nowlez_signup_welcome_v1"


def test_template_registry_is_constructible_via_public_path(pkg):
    """The brief asks: TemplateRegistry().get_template(...) returns a dict-ish.

    Our registry is module-level rather than instance-level, so the public
    path is ``whatsapp_delivery.get_template(...)`` directly. This test
    locks in the brief-specified shape: passing a known template name +
    language returns a non-None object the worker can read.
    """
    t = pkg.get_template("nowlez_hearing_date_change_v1", "en_US")
    assert t is not None
    assert t.full_name == "nowlez_hearing_date_change_v1"
    assert "Hearing date changed" in t.body or "hearing" in t.body.lower()
