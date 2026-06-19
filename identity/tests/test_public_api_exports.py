"""P2.15: the unified-auth bridge functions are importable from the package root."""
import identity


def test_new_bridge_functions_exported():
    for name in (
        "start_wa_login",
        "confirm_wa_login",
        "start_wa_login_from_bot",
        "consume_wa_login",
        "start_email_otp",
        "verify_email_otp_and_login",
        "link_email_to_phone_account",
    ):
        assert name in identity.__all__, f"{name} missing from identity.__all__"
        assert callable(getattr(identity, name)), f"{name} not importable/callable"


def test_mint_helper_reachable_via_api_module():
    # _mint_login_response is a private helper consumed by the casepilot web
    # wrapper as identity.api._mint_login_response — reachable, not in __all__.
    from identity import api as identity_api

    assert callable(identity_api._mint_login_response)
