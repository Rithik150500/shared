"""Static fallback for ``list_states()`` -- the outermost wrapper on the
state-picker method.

The state list is the first step of every guided search and is quasi-immutable,
but the live endpoint shares the IP-wide eCourts throttle. This wrapper makes
the step un-throttleable: it tries the (resilience- and cache-wrapped) live call
first, and only substitutes the baked-in :data:`STATIC_STATES` snapshot when the
live path raises an eCourts error or returns an empty list. A programming bug
(any non-``ECourtsError``) is never masked -- it propagates.

The catch is INTENTIONALLY the whole ``ECourtsError`` family, not just the
throttle subset (RateLimited / CircuitOpen / CourtSiteDown): the state list is
quasi-immutable, so serving the static floor is the right answer for *any*
upstream failure -- a schema change or a geo-block should keep the state step
working just as much as a throttle should. The happy path still fetches live
(and caches it) whenever eCourts recovers, so a genuine future change is picked
up automatically; every fallback also logs a WARNING so a persistent upstream
break stays observable.

Applied OUTSIDE the cache layer in ``_resilience_apply`` so that a cache hit is
returned untouched (fresh live data), the fallback fires only when both cache
and live fail, and an *open circuit* still yields a usable list.
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

from ecourts_client.errors import ECourtsError
from ecourts_client.static_data import STATIC_STATES

logger = logging.getLogger(__name__)


def with_static_state_fallback(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        fallback = STATIC_STATES.get(getattr(self, "scope", None))
        try:
            result = fn(self, *args, **kwargs)
        except ECourtsError:
            if fallback is None:
                # No snapshot for this scope -- we can't help; surface the error.
                raise
            logger.warning(
                "list_states live fetch failed; serving static fallback (scope=%s)",
                getattr(self, "scope", None),
                exc_info=True,
            )
            return list(fallback)
        if not result and fallback is not None:
            logger.warning(
                "list_states returned empty; serving static fallback (scope=%s)",
                getattr(self, "scope", None),
            )
            return list(fallback)
        return result

    return wrapper
