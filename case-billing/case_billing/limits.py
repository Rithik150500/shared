"""Plan case-count limits shared across the Nowlez surfaces (web + WhatsApp bot).

These are plain module constants — deliberately NOT fields on
:class:`case_billing.config.BillingConfig`. ``BillingConfig`` is a
pydantic-settings model whose Razorpay secret fields are *required*, so
``BillingConfig()`` raises when those env vars are unset (e.g. at casepilot
config-import time, or in a plain unit test). A module constant lets both
surfaces import the free-tier cap without standing up the full billing /
secret chain.
"""
from __future__ import annotations

# North Star v2: the free tier allows 5 tracked cases. The count is taken from
# the shared ``cases`` table (see
# ``data_access.daos.case_dao.count_active_cases_for_user``) so the limit is
# UNIFIED across the web app and the WhatsApp bot — one case-book, one quota,
# 5 cases total (not 5-per-surface). Both surfaces import THIS constant so the
# number can never skew between them.
NOWLEZ_FREE_TIER_CASE_CAP: int = 5
