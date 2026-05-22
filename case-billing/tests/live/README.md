# Live tests — Razorpay sandbox

These tests make real API calls to Razorpay's sandbox. They're skipped by default and only run when invoked with the `live` marker.

## Running

```bash
export RAZORPAY_SANDBOX_API_KEY=...
export RAZORPAY_SANDBOX_API_SECRET=...
export RAZORPAY_SANDBOX_WEBHOOK_SECRET=...

cd C:\Project3\shared\case-billing
pytest -m live tests/live/ -v
```

## What they cover

- `test_razorpay_sandbox_e2e.py` (Task 2.1) — exercises invoice generation → hand-crafted webhook → DB state flip → WhatsApp enqueue → idempotency replay.

## Pre-launch checklist

Before flipping `MUNSHI_BILLING_ENABLED=true`:

- [ ] All live tests pass.
- [ ] One manual smoke with a real Razorpay sandbox card payment + temporary tunnel (verifies Razorpay's actual webhook delivery, which the hand-crafted test bypasses).
- [ ] Production webhook URL configured in Razorpay live-mode dashboard.
- [ ] `RAZORPAY_WEBHOOK_SECRET` set in Munshi's Railway env.
