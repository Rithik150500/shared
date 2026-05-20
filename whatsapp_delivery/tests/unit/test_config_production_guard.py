"""Production-guard tests for WhatsAppConfig.

Audit follow-up (May 2026 incident): ``shared_redis_url`` defaults to
``redis://redis:6379/0`` (the docker-compose service name) which silently
broke on Railway. The hard-fail guard in ``model_post_init`` raises if
the default URL is active AND a production-indicator env var is set.

Test matrix:
- default URL + no prod indicator → INFO log, no raise (dev path)
- default URL + RAILWAY_ENVIRONMENT set → RuntimeError
- default URL + IS_PRODUCTION set → RuntimeError
- default URL + ENV=production set → RuntimeError
- overridden URL + RAILWAY_ENVIRONMENT set → no raise (operator set it)
- overridden URL + no prod indicator → no raise, no INFO log
"""
from __future__ import annotations

import logging

import pytest

from whatsapp_delivery.config import WhatsAppConfig, _DEV_DEFAULT_REDIS_URL


# All tests need the Meta env vars set (those are required fields).
@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    # Default-clean state: clear all prod indicators + the URL override.
    for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "IS_PRODUCTION",
              "ENV", "SHARED_REDIS_URL"):
        monkeypatch.delenv(k, raising=False)


def test_default_url_in_dev_logs_but_does_not_raise(caplog):
    """No prod indicator + default URL → INFO log, no raise."""
    with caplog.at_level(logging.INFO, logger="whatsapp_delivery.config"):
        cfg = WhatsAppConfig()
    assert cfg.shared_redis_url == _DEV_DEFAULT_REDIS_URL
    assert any(
        "shared_redis_url using dev default" in r.message for r in caplog.records
    )


def test_default_url_with_railway_environment_raises(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="shared_redis_url.*production"):
        WhatsAppConfig()


def test_default_url_with_railway_project_id_raises(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "abc-123")
    with pytest.raises(RuntimeError, match="shared_redis_url.*production"):
        WhatsAppConfig()


def test_default_url_with_is_production_raises(monkeypatch):
    monkeypatch.setenv("IS_PRODUCTION", "true")
    with pytest.raises(RuntimeError, match="shared_redis_url.*production"):
        WhatsAppConfig()


def test_default_url_with_env_production_raises(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError, match="shared_redis_url.*production"):
        WhatsAppConfig()


def test_default_url_with_env_production_case_insensitive(monkeypatch):
    monkeypatch.setenv("ENV", "PRODUCTION")
    with pytest.raises(RuntimeError):
        WhatsAppConfig()


def test_overridden_url_in_prod_does_not_raise(monkeypatch):
    """When operator explicitly sets SHARED_REDIS_URL, no guard fires."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SHARED_REDIS_URL", "redis://my-real-redis.example.com:6379/0")
    cfg = WhatsAppConfig()
    assert cfg.shared_redis_url == "redis://my-real-redis.example.com:6379/0"


def test_overridden_url_in_dev_emits_no_default_warning(monkeypatch, caplog):
    monkeypatch.setenv("SHARED_REDIS_URL", "redis://elsewhere:6379/0")
    with caplog.at_level(logging.INFO, logger="whatsapp_delivery.config"):
        cfg = WhatsAppConfig()
    assert cfg.shared_redis_url == "redis://elsewhere:6379/0"
    assert not any(
        "shared_redis_url using dev default" in r.message for r in caplog.records
    )


def test_empty_prod_indicator_is_treated_as_unset(monkeypatch):
    """A blank-string env var should not be treated as 'production'."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "")
    monkeypatch.setenv("IS_PRODUCTION", "   ")
    cfg = WhatsAppConfig()  # Should NOT raise
    assert cfg.shared_redis_url == _DEV_DEFAULT_REDIS_URL
