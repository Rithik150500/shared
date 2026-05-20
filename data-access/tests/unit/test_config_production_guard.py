"""Production-guard tests for DataAccessSettings.

Audit follow-up (May 2026 incident): ``DATABASE_URL`` defaulted to
``postgresql+psycopg2://localhost/nowlez_munshi_shared`` which silently
broke on Railway — casepilot's scheduler crons crashed daily for weeks
before the gap was found during an unrelated investigation. The
``model_post_init`` guard now raises if the default URL is active AND
a production-indicator env var is set.

Note on test structure: ``data_access.config`` has a module-bottom
``settings = DataAccessSettings()`` so the guard fires at IMPORT time
in production — which is the correct deploy-time-vs-incident-time
trade-off. The tests therefore have to either:
  (a) check that the import itself raises (for the raise-expected cases)
  (b) reload the module after setting overrides (for the no-raise cases)

Both helpers below are pytest-friendly.
"""
from __future__ import annotations

import importlib
import logging
import sys

import pytest


def _reimport_config():
    """Pop the cached module + re-import — runs the module body fresh.

    Used for both raise-expected and no-raise tests; raises propagate to
    the caller naturally.
    """
    sys.modules.pop("data_access.config", None)
    return importlib.import_module("data_access.config")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "IS_PRODUCTION",
              "ENV", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_default_url_in_dev_logs_but_does_not_raise(caplog):
    with caplog.at_level(logging.INFO, logger="data_access.config"):
        mod = _reimport_config()
    assert mod.settings.DATABASE_URL == mod._DEV_DEFAULT_DATABASE_URL
    assert any(
        "DATABASE_URL using dev default" in r.message for r in caplog.records
    )


def test_default_url_with_railway_environment_raises(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="DATABASE_URL.*production"):
        _reimport_config()


def test_default_url_with_railway_project_id_raises(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-123")
    with pytest.raises(RuntimeError, match="DATABASE_URL.*production"):
        _reimport_config()


def test_default_url_with_is_production_raises(monkeypatch):
    monkeypatch.setenv("IS_PRODUCTION", "true")
    with pytest.raises(RuntimeError, match="DATABASE_URL.*production"):
        _reimport_config()


def test_default_url_with_env_production_raises(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError, match="DATABASE_URL.*production"):
        _reimport_config()


def test_default_url_with_env_production_case_insensitive(monkeypatch):
    monkeypatch.setenv("ENV", "PRODUCTION")
    with pytest.raises(RuntimeError):
        _reimport_config()


def test_overridden_url_in_prod_does_not_raise(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://real-host.example.com:5432/db")
    mod = _reimport_config()
    assert mod.settings.DATABASE_URL == "postgresql://real-host.example.com:5432/db"


def test_overridden_url_in_dev_emits_no_default_warning(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere:5432/db")
    with caplog.at_level(logging.INFO, logger="data_access.config"):
        mod = _reimport_config()
    assert mod.settings.DATABASE_URL == "postgresql://elsewhere:5432/db"
    assert not any(
        "DATABASE_URL using dev default" in r.message for r in caplog.records
    )


def test_empty_prod_indicator_is_treated_as_unset(monkeypatch):
    """A blank-string env var should not be treated as 'production'."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "")
    monkeypatch.setenv("IS_PRODUCTION", "   ")
    mod = _reimport_config()
    assert mod.settings.DATABASE_URL == mod._DEV_DEFAULT_DATABASE_URL
