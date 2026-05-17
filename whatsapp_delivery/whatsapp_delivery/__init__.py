"""Shared WhatsApp Cloud API delivery client.

Public API re-exports are appended in Task 8 after all submodules land.
"""
__version__ = "0.1.0"

try:
    import sentry_sdk
    sentry_sdk.set_tag("package", "whatsapp_delivery")
except ImportError:
    pass
