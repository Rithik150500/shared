"""Public API surface for the Nowlez subscription flow (Task 7.7).

Re-exports the canonical functions from the submodules so consumers
(backend routers, webhook handlers, admin tools) import from a single
predictable namespace. The stable verbs:

* Trial: :func:`create_trial_for_new_signup`, :func:`is_in_trial`,
  :func:`days_remaining_in_trial`, :func:`start_trial`,
  :func:`expire_trial`, :func:`trial_ends_at_for`.
* Tier selection: :func:`select_tier`, :func:`select_tier_and_subscribe`.
* Subscription lifecycle: :func:`activate_subscription`,
  :func:`mark_past_due`.
* Intro promo: :func:`get_intro_offer_id`,
  :func:`transition_intro_promo_state`,
  :func:`record_intro_promo_consumed`, :func:`record_intro_promo_skipped`.
* Referral: :func:`find_or_create_referral`,
  :func:`apply_referree_2nd_month_discount`,
  :func:`schedule_referrer_mutual_benefit`,
  :func:`mark_referrer_mutual_applied`,
  :func:`expire_referral`,
  :func:`apply_referral_at_first_payment`.
* Fallback: :func:`fallback_to_munshi`, :func:`freeze_account`.
* Cancellation: :func:`cancel_subscription`, :func:`downgrade_subscription`.
"""

from __future__ import annotations

from case_billing.nowlez.cancellation import (
    cancel_subscription,
    downgrade_subscription,
)
from case_billing.nowlez.fallback import (
    fallback_to_munshi,
    freeze_account,
)
from case_billing.nowlez.promos import (
    get_intro_offer_id,
    record_intro_promo_consumed,
    record_intro_promo_skipped,
    transition_intro_promo_state,
)
from case_billing.nowlez.referrals import (
    apply_referral_at_first_payment,
    apply_referree_2nd_month_discount,
    expire_referral,
    find_or_create_referral,
    mark_referrer_mutual_applied,
    schedule_referrer_mutual_benefit,
)
from case_billing.nowlez.subscriptions import (
    activate_subscription,
    mark_past_due,
    select_tier,
    select_tier_and_subscribe,
)
from case_billing.nowlez.trial import (
    create_trial_for_new_signup,
    days_remaining_in_trial,
    expire_trial,
    is_in_trial,
    start_trial,
    trial_ends_at_for,
)


__all__ = [
    # Trial
    "create_trial_for_new_signup",
    "is_in_trial",
    "days_remaining_in_trial",
    "start_trial",
    "expire_trial",
    "trial_ends_at_for",
    # Tier selection / subscription lifecycle
    "select_tier",
    "select_tier_and_subscribe",
    "activate_subscription",
    "mark_past_due",
    # Intro promo
    "get_intro_offer_id",
    "transition_intro_promo_state",
    "record_intro_promo_consumed",
    "record_intro_promo_skipped",
    # Referral
    "find_or_create_referral",
    "apply_referral_at_first_payment",
    "apply_referree_2nd_month_discount",
    "schedule_referrer_mutual_benefit",
    "mark_referrer_mutual_applied",
    "expire_referral",
    # Fallback
    "fallback_to_munshi",
    "freeze_account",
    # Cancellation
    "cancel_subscription",
    "downgrade_subscription",
]
