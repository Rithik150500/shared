from identity.errors import (
    DeliveryFailed,
    IdentityError,
    InvalidCredentials,
    InvalidToken,
    OtpAlreadyUsed,
    OtpAttemptsExhausted,
    OtpExpired,
    OtpInvalid,
    PasswordNotSet,
    PasswordTooWeak,
    RateLimited,
    SessionExpired,
    SessionRevoked,
)
from identity.config import settings


def test_error_hierarchy():
    # All identity exceptions inherit from the base IdentityError
    for exc in (OtpExpired, OtpAlreadyUsed, OtpAttemptsExhausted, OtpInvalid,
                RateLimited, InvalidToken, SessionRevoked, SessionExpired,
                InvalidCredentials, PasswordNotSet, PasswordTooWeak):
        assert issubclass(exc, IdentityError)
    assert issubclass(DeliveryFailed, IdentityError)


def test_rate_limited_carries_retry_after():
    e = RateLimited(retry_after_seconds=120)
    assert e.retry_after_seconds == 120


def test_delivery_failed_carries_channel():
    e = DeliveryFailed("whatsapp", "timeout")
    assert e.channel == "whatsapp"
    assert e.provider_error == "timeout"


def test_settings_defaults():
    assert settings.JWT_ALGORITHM == "HS256"
    assert settings.JWT_ACCESS_TTL_MINUTES == 30
    assert settings.REFRESH_TTL_DAYS == 30
    assert settings.OTP_TTL_MINUTES == 10
    assert settings.OTP_LENGTH == 6
    assert settings.OTP_MAX_ATTEMPTS == 3
    # Rate limits per spec
    assert settings.OTP_PER_PHONE_PER_HOUR == 5
    assert settings.OTP_PER_IP_PER_HOUR == 20
    # argon2 params per OWASP 2026
    assert settings.ARGON2_TIME_COST == 2
    assert settings.ARGON2_MEMORY_COST_KB == 65536
    assert settings.ARGON2_PARALLELISM == 4


def test_settings_jwt_secret_env_override(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-override")
    from importlib import reload
    import identity.config as cfg
    reload(cfg)
    assert cfg.settings.JWT_SECRET_KEY == "test-secret-override"
