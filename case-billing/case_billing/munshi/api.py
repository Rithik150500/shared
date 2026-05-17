"""Public API surface for the Munshi billing flow (spec Section 2.2).

Consumers (Munshi cron worker, webhook router, admin tools) import from
this module rather than reaching into the submodules so we have a
single, observable contract that can be versioned without breaking
callers. The re-exports below pair each submodule function with the
verb-noun name the spec uses in its sequence diagrams.

Stable names for the spec's "Munshi billing flow" verbs:

* :func:`record_case_billable_start` — alias for `open_billing_period`.
* :func:`record_case_billable_end`   — alias for `close_billing_period`.
* :func:`count_billable_cases_in_window` — DISTINCT-case counter.
* :func:`generate_anniversary_invoice` — cron's per-user-per-cycle entry.
* :func:`handle_invoice_paid`        — webhook handler.
* :func:`void_invoice`               — mid-cycle void path (sub-project C).
* :func:`suspend_user` / :func:`resume_user` — grace transitions.
* :func:`enter_grace_period`         — alias for `send_payment_reminder`
  (the spec calls the day-of/past-due reminder "entering grace").
"""

from __future__ import annotations

from case_billing.munshi.invoices import (
    generate_anniversary_invoice,
    mark_invoice_paid as handle_invoice_paid,
    void_invoice,
)
from case_billing.munshi.suspension import (
    resume_user,
    send_payment_reminder as enter_grace_period,
    suspend_user,
)
from case_billing.munshi.usage import (
    close_billing_period as record_case_billable_end,
    count_billable_cases_in_window,
    open_billing_period as record_case_billable_start,
)


__all__ = [
    "count_billable_cases_in_window",
    "enter_grace_period",
    "generate_anniversary_invoice",
    "handle_invoice_paid",
    "record_case_billable_end",
    "record_case_billable_start",
    "resume_user",
    "suspend_user",
    "void_invoice",
]
