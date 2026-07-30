"""Consumer forum (e-Jagriti) adapter package.

Registered as the ``Forum.CONSUMER`` adapter in ``ecourts_client/__init__.py``.
"""
from ecourts_client.consumer.client import (
    NCDRC_COMMISSION,
    NCDRC_COMMISSION_ID,
    SEARCH_ROLES,
    ConsumerClient,
)
from ecourts_client.consumer.models import CommissionRef

__all__ = [
    "NCDRC_COMMISSION",
    "NCDRC_COMMISSION_ID",
    "SEARCH_ROLES",
    "CommissionRef",
    "ConsumerClient",
]
