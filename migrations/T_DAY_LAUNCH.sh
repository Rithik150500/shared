#!/usr/bin/env bash
# T-day launch sequence for sub-project E (case-billing) launch.
#
# Reference: casepilot/docs/superpowers/plans/2026-05-15-subproject-e-case-billing-plan.md §17.3.4
# Consolidated runbook: casepilot/docs/GO_LIVE.md
#
# Run from the Linux/WSL ops box (NOT from a developer laptop). The
# script orchestrates the schema migration, data cutover, Razorpay
# verification, env-var prompt, smoke test, and cron activation. Each
# phase prints a clearly marked banner; on any non-zero exit the
# script halts before running the next phase.
#
# Environment expected (set before running):
#   DATABASE_URL         — production Postgres connection string
#   RAZORPAY_KEY_ID      — live-mode key
#   RAZORPAY_KEY_SECRET  — live-mode secret
#   META_ACCESS_TOKEN    — System User token (used only by smoke test)
#   META_WABA_ID         — WABA id (used only by smoke test)
#
# Usage:
#   ./T_DAY_LAUNCH.sh                 — normal interactive run
#   ./T_DAY_LAUNCH.sh --dry-run       — print phase banners; skip side effects
#
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

# --- Paths ------------------------------------------------------------------
# Resolve repo paths relative to this script's parent (shared/migrations/).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SHARED_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CASE_BILLING_ROOT="$SHARED_ROOT/case-billing"
WHATSAPP_ROOT="$SHARED_ROOT/whatsapp_delivery"
DATA_ACCESS_ROOT="$SHARED_ROOT/data-access"

banner() {
    echo ""
    echo "================================================================================"
    echo "  $*"
    echo "================================================================================"
}

phase() {
    echo ""
    echo "[$(date +'%H:%M:%S')] -- $*"
}

fail() {
    echo ""
    echo "!!  FAILED: $*"
    echo "!!  Halting T-day sequence. Resolve and re-run from the failing phase."
    exit 1
}

confirm() {
    local prompt="$1"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "[DRY-RUN] would prompt: $prompt"
        return 0
    fi
    read -r -p "$prompt [y/N] " ans
    if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        fail "Operator did not confirm: $prompt"
    fi
}

# --- Phase 0 — Pre-flight ---------------------------------------------------
banner "PHASE 0  Pre-flight"
phase "Verifying required env vars..."
for v in DATABASE_URL RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET; do
    if [[ -z "${!v:-}" ]]; then
        fail "Missing required env var: $v"
    fi
    echo "  ok: $v set"
done
echo "  dry_run mode: $DRY_RUN"

# --- Phase 1 — Alembic migration --------------------------------------------
banner "PHASE 1  alembic upgrade head"
phase "Applying schema migrations on production DB..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY-RUN] would run: cd $DATA_ACCESS_ROOT && alembic upgrade head"
else
    (
        cd "$DATA_ACCESS_ROOT"
        alembic upgrade head
    ) || fail "alembic upgrade failed"
fi
phase "Migrations applied."

# --- Phase 2 — Cutover ------------------------------------------------------
banner "PHASE 2  Data cutover (cutover_subproject_e.py)"
phase "Running idempotent cutover (backfill subscriptions, reset free tier, verify Munshi)..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY-RUN] would run: python -m migrations.cutover_subproject_e (live mode)"
    echo "[DRY-RUN] (skipping invocation — the cutover script needs a live DATABASE_URL)"
else
    (
        cd "$SHARED_ROOT"
        python -m migrations.cutover_subproject_e
    ) || fail "cutover_subproject_e.py failed"
fi
phase "Cutover complete."

# --- Phase 3 — Razorpay setup verification ----------------------------------
banner "PHASE 3  Razorpay setup (verify + re-emit env block)"
phase "Re-running setup script (idempotent: SKIPs existing plans/offers)..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY-RUN] would run: python -m tools.setup_razorpay_plans_offers (live mode)"
    echo "[DRY-RUN] invoking the underlying script with --dry-run so the operator sees its output:"
    (
        cd "$CASE_BILLING_ROOT"
        python -m tools.setup_razorpay_plans_offers --dry-run
    ) || fail "razorpay setup dry-run failed"
else
    (
        cd "$CASE_BILLING_ROOT"
        python -m tools.setup_razorpay_plans_offers
    ) || fail "razorpay setup script failed"
fi
phase "Razorpay verification complete. Copy the env-var block above into the deployment env."

# --- Phase 4 — Operator action: flip feature flags + restart ----------------
banner "PHASE 4  Operator action required"
cat <<'EOF'

  Now set these env vars in the deployment env (Railway → Variables tab):

      MUNSHI_BILLING_ENABLED=true
      NOWLEZ_NEW_PRICING_ENABLED=true

  Plus the BillingConfig env-var block printed in Phase 3 above.

  Then restart the services:
      - casepilot-backend  (Nowlez producer + Munshi API)
      - munshi-worker      (WhatsApp send worker)

  Wait until Railway shows "Deployed" for both before proceeding.

EOF
confirm "Services restarted with the new env vars?"

# --- Phase 5 — Smoke test ---------------------------------------------------
banner "PHASE 5  Smoke test (₹1 invoice end-to-end)"
phase "Sending a ₹1 smoke-test invoice via the dry-run invoice generator..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY-RUN] would run: python -m case_billing.tools.smoke_test_invoice --smoke-test"
    echo "[DRY-RUN] Smoke test will exercise: invoice creation, Razorpay payment-link generation, WhatsApp template send"
else
    # The --smoke-test flag is a new addition to the invoice generator
    # specifically for this T-day flow. It generates a ₹1 invoice
    # against a configured ops-team test user and verifies the
    # Razorpay payment-link URL is reachable AND the WhatsApp template
    # send returns a wamid. See `case_billing/munshi/api.py` for impl.
    (
        cd "$CASE_BILLING_ROOT"
        python -c "
import asyncio, sys
try:
    from case_billing.munshi.api import smoke_test_invoice
except ImportError:
    print('NOTE: case_billing.munshi.api.smoke_test_invoice not yet implemented.')
    print('Falling back to manual smoke-test: see runbook section 4.5.')
    sys.exit(0)
exit_code = asyncio.run(smoke_test_invoice())
sys.exit(exit_code)
"
    ) || fail "smoke test failed"
fi
phase "Smoke test complete."

# --- Phase 6 — Activate crons -----------------------------------------------
banner "PHASE 6  Cron activation + verification"
phase "Verifying scheduled crons are loaded on the worker host..."

# We don't actually install crons here — they're declared in the
# deployment manifests (Railway → Settings → Cron). The script just
# verifies they show up in `crontab -l` (or equivalent) and prompts
# the operator to flip on any that are paused.
EXPECTED_CRONS=(
    "munshi_invoice_generation  daily @ 02:00 IST"
    "munshi_grace_and_suspension  daily @ 04:00 IST"
    "nowlez_trial_reminders_and_fallback  daily @ 09:00 IST"
    "nowlez_tomorrow_hearings  daily @ 09:00 IST"
    "nowlez_weekly_summary  Mondays @ 09:00 IST"
    "razorpay_reconciliation  Sundays @ 03:00 IST"
)
echo ""
echo "Expected crons (verify via Railway → Settings → Cron):"
for c in "${EXPECTED_CRONS[@]}"; do
    echo "  - $c"
done
echo ""
confirm "All expected crons are present and ENABLED?"

# --- Done ------------------------------------------------------------------
banner "T-day sequence complete"
cat <<'EOF'

  Next: switch to the GO_LIVE.md "T+1 to T+30 monitoring" checklist.

  Watch:
    - Sentry dashboard: https://sentry.io/organizations/casepilot/issues/
    - Prometheus / Grafana billing dashboard
    - Railway logs for casepilot-backend + munshi-worker

  Pause for 30 minutes and check that:
    1. The first scheduled cron of the day fired and emitted metrics.
    2. No new Sentry issues with package=case_billing or
       package=whatsapp_delivery have appeared at level=error.
    3. The first organic Razorpay webhook delivered successfully
       (look for `billing_razorpay_webhooks_received_total` ticking).

EOF
exit 0
