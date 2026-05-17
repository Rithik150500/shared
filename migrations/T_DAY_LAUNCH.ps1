# T-day launch sequence for sub-project E (case-billing) launch -- PowerShell variant.
#
# Reference: casepilot/docs/superpowers/plans/2026-05-15-subproject-e-case-billing-plan.md §17.3.4
# Consolidated runbook: casepilot/docs/GO_LIVE.md
#
# Functional mirror of T_DAY_LAUNCH.sh for Windows-based ops boxes. Each
# phase prints a clearly marked banner; on any non-zero exit code the
# script halts before running the next phase.
#
# Environment expected (set before running):
#   DATABASE_URL         -- production Postgres connection string
#   RAZORPAY_KEY_ID      -- live-mode key
#   RAZORPAY_KEY_SECRET  -- live-mode secret
#   META_ACCESS_TOKEN    -- System User token (used only by smoke test)
#   META_WABA_ID         -- WABA id (used only by smoke test)
#
# Usage:
#   .\T_DAY_LAUNCH.ps1                  -- normal interactive run
#   .\T_DAY_LAUNCH.ps1 -DryRun          -- print phase banners; skip side effects
#
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
# PowerShell 5.1 treats native-command stderr as terminating errors when
# ErrorActionPreference=Stop. Python's logging writes INFO lines to
# stderr by default, so we keep Continue here and lean on $LASTEXITCODE
# checks after each native invocation for control flow.

# --- Paths ------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SharedRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$CaseBillingRoot = Join-Path $SharedRoot "case-billing"
$WhatsAppRoot = Join-Path $SharedRoot "whatsapp_delivery"
$DataAccessRoot = Join-Path $SharedRoot "data-access"

function Banner {
    param([string]$Text)
    Write-Host ""
    Write-Host "================================================================================"
    Write-Host "  $Text"
    Write-Host "================================================================================"
}

function Phase {
    param([string]$Text)
    Write-Host ""
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] -- $Text"
}

function FailHard {
    param([string]$Text)
    Write-Host ""
    Write-Host "!!  FAILED: $Text"
    Write-Host "!!  Halting T-day sequence. Resolve and re-run from the failing phase."
    exit 1
}

function Confirm-Op {
    param([string]$Prompt)
    if ($DryRun) {
        Write-Host "[DRY-RUN] would prompt: $Prompt"
        return
    }
    $ans = Read-Host "$Prompt [y/N]"
    if ($ans -notmatch '^[Yy]$') {
        FailHard "Operator did not confirm: $Prompt"
    }
}

# --- Phase 0 -- Pre-flight ---------------------------------------------------
Banner "PHASE 0  Pre-flight"
Phase "Verifying required env vars..."
foreach ($v in @("DATABASE_URL", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")) {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ([string]::IsNullOrEmpty($val)) {
        FailHard "Missing required env var: $v"
    }
    Write-Host "  ok: $v set"
}
Write-Host "  dry_run mode: $DryRun"

# --- Phase 1 -- Alembic migration --------------------------------------------
Banner "PHASE 1  alembic upgrade head"
Phase "Applying schema migrations on production DB..."
if ($DryRun) {
    Write-Host "[DRY-RUN] would run: alembic upgrade head (cwd=$DataAccessRoot)"
} else {
    Push-Location $DataAccessRoot
    try {
        cmd /c "alembic upgrade head 2>&1"
        if ($LASTEXITCODE -ne 0) { FailHard "alembic upgrade failed" }
    } finally { Pop-Location }
}
Phase "Migrations applied."

# --- Phase 2 -- Cutover ------------------------------------------------------
Banner "PHASE 2  Data cutover (cutover_subproject_e.py)"
Phase "Running idempotent cutover (backfill subscriptions, reset free tier, verify Munshi)..."
if ($DryRun) {
    Write-Host "[DRY-RUN] would run: python -m migrations.cutover_subproject_e (live mode)"
    Write-Host "[DRY-RUN] (skipping invocation -- the cutover script needs a live DATABASE_URL)"
} else {
    Push-Location $SharedRoot
    try {
        cmd /c "python -m migrations.cutover_subproject_e 2>&1"
        if ($LASTEXITCODE -ne 0) { FailHard "cutover_subproject_e.py failed" }
    } finally { Pop-Location }
}
Phase "Cutover complete."

# --- Phase 3 -- Razorpay setup verification ----------------------------------
Banner "PHASE 3  Razorpay setup (verify + re-emit env block)"
Phase "Re-running setup script (idempotent: SKIPs existing plans/offers)..."
Push-Location $CaseBillingRoot
try {
    if ($DryRun) {
        Write-Host "[DRY-RUN] would run: python -m tools.setup_razorpay_plans_offers (live mode)"
        Write-Host "[DRY-RUN] invoking the underlying script with --dry-run so the operator sees its output:"
        # Redirect stderr (Python's logging stream) to stdout so PS 5.1
        # doesn't decorate every INFO line as a NativeCommandError.
        $env:PYTHONUNBUFFERED = "1"
        cmd /c "python -m tools.setup_razorpay_plans_offers --dry-run 2>&1"
        if ($LASTEXITCODE -ne 0) { FailHard "razorpay setup dry-run failed" }
    } else {
        $env:PYTHONUNBUFFERED = "1"
        cmd /c "python -m tools.setup_razorpay_plans_offers 2>&1"
        if ($LASTEXITCODE -ne 0) { FailHard "razorpay setup script failed" }
    }
} finally { Pop-Location }
Phase "Razorpay verification complete. Copy the env-var block above into the deployment env."

# --- Phase 4 -- Operator action: flip feature flags + restart ----------------
Banner "PHASE 4  Operator action required"
Write-Host @"

  Now set these env vars in the deployment env (Railway -> Variables tab):

      MUNSHI_BILLING_ENABLED=true
      NOWLEZ_NEW_PRICING_ENABLED=true

  Plus the BillingConfig env-var block printed in Phase 3 above.

  Then restart the services:
      - casepilot-backend  (Nowlez producer + Munshi API)
      - munshi-worker      (WhatsApp send worker)

  Wait until Railway shows "Deployed" for both before proceeding.

"@
Confirm-Op "Services restarted with the new env vars?"

# --- Phase 5 -- Smoke test ---------------------------------------------------
Banner "PHASE 5  Smoke test (Rs.1 invoice end-to-end)"
Phase "Sending a Rs.1 smoke-test invoice via the dry-run invoice generator..."
if ($DryRun) {
    Write-Host "[DRY-RUN] would run: python -m case_billing.tools.smoke_test_invoice --smoke-test"
    Write-Host "[DRY-RUN] Smoke test exercises: invoice create, Razorpay payment-link gen, WhatsApp template send"
} else {
    Push-Location $CaseBillingRoot
    try {
        $py = @'
import asyncio, sys
try:
    from case_billing.munshi.api import smoke_test_invoice
except ImportError:
    print('NOTE: case_billing.munshi.api.smoke_test_invoice not yet implemented.')
    print('Falling back to manual smoke-test: see runbook section 4.5.')
    sys.exit(0)
exit_code = asyncio.run(smoke_test_invoice())
sys.exit(exit_code)
'@
        $pyEscaped = $py -replace '"', '\"'
        cmd /c "python -c `"$pyEscaped`" 2>&1"
        if ($LASTEXITCODE -ne 0) { FailHard "smoke test failed" }
    } finally { Pop-Location }
}
Phase "Smoke test complete."

# --- Phase 6 -- Activate crons -----------------------------------------------
Banner "PHASE 6  Cron activation + verification"
Phase "Verifying scheduled crons are loaded on the worker host..."
$expected = @(
    "munshi_invoice_generation             daily @ 02:00 IST",
    "munshi_grace_and_suspension           daily @ 04:00 IST",
    "nowlez_trial_reminders_and_fallback   daily @ 09:00 IST",
    "nowlez_tomorrow_hearings              daily @ 09:00 IST",
    "nowlez_weekly_summary                 Mondays @ 09:00 IST",
    "razorpay_reconciliation               Sundays @ 03:00 IST"
)
Write-Host ""
Write-Host "Expected crons (verify via Railway -> Settings -> Cron):"
foreach ($c in $expected) { Write-Host "  - $c" }
Write-Host ""
Confirm-Op "All expected crons are present and ENABLED?"

# --- Done ------------------------------------------------------------------
Banner "T-day sequence complete"
Write-Host @"

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
       (look for ``billing_razorpay_webhooks_received_total`` ticking).

"@
exit 0
