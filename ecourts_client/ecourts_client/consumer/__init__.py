"""Consumer forum (e-Jagriti) adapter package.

Registered as the ``Forum.CONSUMER`` adapter in ``ecourts_client/__init__.py``.
"""
from ecourts_client.consumer.client import ConsumerClient
from ecourts_client.consumer.models import CommissionRef

__all__ = ["CommissionRef", "ConsumerClient"]
