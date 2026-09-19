"""Module-level cache backend: injected once, read many, resettable for tests."""
from __future__ import annotations

import pytest

from ecourts_client.cache.backend import clear_backend, get_backend, set_backend


@pytest.fixture(autouse=True)
def _reset():
    clear_backend()
    yield
    clear_backend()


def test_default_is_none():
    assert get_backend() is None


def test_set_then_get():
    sentinel = object()
    set_backend(sentinel)
    assert get_backend() is sentinel


def test_clear_resets_to_none():
    set_backend(object())
    clear_backend()
    assert get_backend() is None
