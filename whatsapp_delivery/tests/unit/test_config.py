import pytest
from whatsapp_delivery.config import WhatsAppConfig


def test_config_constructs_from_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    cfg = WhatsAppConfig()
    assert cfg.meta_phone_number_id == "111"
    assert cfg.idempotency_window_seconds == 86400 * 7
    assert cfg.whatsapp_nowlez_disabled is False


def test_config_missing_required_raises(monkeypatch):
    monkeypatch.delenv("META_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    with pytest.raises(Exception):
        WhatsAppConfig()
