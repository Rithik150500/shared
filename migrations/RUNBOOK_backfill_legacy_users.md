# Runbook — backfill_legacy_users (Sub-project A R2)

Migrates the real legacy SQLite users into Postgres `users` + `users_nowlez`,
keyed on email, recording the source id in `users_nowlez.legacy_sqlite_id`.
One-shot, idempotent, has `--dry-run`.

**Target size:** ~70 users (NOT 1,729 — see audit in the script docstring;
~1,658 SQLite rows are integration-test fixtures).

---

## Prerequisites (in order)

1. **`users.email` Alembic migration applied** to prod Postgres (plan Task 4).
   The script INSERTs `User(email=...)`; the column must exist.
2. **`shared` pin bumped** in `casepilot/requirements.txt` to a SHA that
   includes the `users.email` migration, this script, AND the Sub-G I5
   `users_nowlez.legacy_sqlite_id` column (commit `15dd86de`) — plan Task 5.
   The current pin `e01843e` predates `legacy_sqlite_id`, so the backfill
   crashes on row 1 (`AttributeError: UserNowlez has no attribute
   legacy_sqlite_id`). Confirmed empirically 2026-05-28 — the local test env
   hit exactly this because its `data_access` is the same `e01843e` install.
3. **Snapshot of the legacy SQLite DB** captured locally (see Step 1).

> ⚠️ **Source moved.** The plan's Task 2 snapshots via `railway ssh`, but prod
> migrated to the DigitalOcean droplet on 2026-05-27 (Railway decommissions
> ~2026-06-03). Snapshot from the **droplet**, not Railway.

---

## Step 1 — Snapshot the legacy SQLite (read-only)

A snapshot already exists at
`casepilot/.worktrees/finish-pr6-cutover/.local/local-prod-app.db` (1,731 rows,
pulled 2026-05-2x). Re-pull only if you need a fresher copy:

```bash
# Verify the container name + in-container path first (read-only):
ssh case-tracker "docker ps --format '{{.Names}}' | grep casepilot"
ssh case-tracker "docker exec deploy-casepilot-1 ls -la /app/backend/storage/app.db"

# Then copy it out:
ssh case-tracker "docker exec deploy-casepilot-1 cat /app/backend/storage/app.db" \
  > .local/local-prod-app.db
```

Confirm it parses and has the expected magnitude:

```bash
python -c "import sqlite3;print(sqlite3.connect('.local/local-prod-app.db').execute('select count(*) from users').fetchone())"
# expect ~1731
```

## Step 2 — Dry run (no writes)

```bash
cd C:/Project3/shared
python -m migrations.backfill_legacy_users \
  --sqlite-path /path/to/.local/local-prod-app.db --dry-run
```

**Gate:** the summary must show `candidates` ≈ **70** and sit inside the band
`[40, 200]`. If it's ~1,731, the test-row denylist didn't apply — STOP and
investigate before any live run.

## Step 3 — Eyeball the flagged accounts

Review `migration-prep/legacy-user-review-2026-05-28.csv` (`flag` column). The
9 heuristic-flagged rows are already adjudicated in `_MANUAL_DROP_EMAILS`
(3 dropped) + documented keeps. Override the drop set in the script if you
disagree, then re-run Step 2.

- `meeran.navj@gmai.com` is **kept but undeliverable** (typo of `gmail.com`).
  Note it for support follow-up; do not send it transactional email.

## Step 4 — Live run

```bash
python -m migrations.backfill_legacy_users \
  --sqlite-path /path/to/.local/local-prod-app.db
# summary: {"candidates": ~70, "inserted": N, "already_linked": 0,
#           "email_conflict": M, "dry_run": false}
```

Re-running is safe (idempotent): a second run reports `inserted: 0`,
`already_linked: ~70`.

## Step 5 — Post-run verification

```sql
-- migrated identities present:
SELECT count(*) FROM users_nowlez WHERE legacy_sqlite_id IS NOT NULL;   -- ≈ inserted
-- no orphan users_nowlez (every one has a users row):
SELECT count(*) FROM users_nowlez n LEFT JOIN users u ON u.id = n.user_id WHERE u.id IS NULL;  -- 0
```

Then smoke a known migrated user end-to-end: log in with their email/password
(carried over) and confirm the dashboard loads.

---

## Rollback

The backfill is purely additive. To undo (pre-launch only, no dependent data):

```sql
DELETE FROM users_nowlez WHERE legacy_sqlite_id IS NOT NULL;
DELETE FROM users WHERE id NOT IN (SELECT user_id FROM users_nowlez)
                   AND id NOT IN (SELECT user_id FROM users_munshi);
```

(Adjust if other tables now FK into the back-filled `users.id`.)

## Known caveats / follow-ups

- **Referral graph not remapped.** `referred_by` / `referral_code` are not
  carried (FK points to Postgres UUIDs that don't exist mid-migration). Do a
  second pass after this backfill if referrals matter pre-launch.
- **Tier policy = carry-verbatim.** Legacy `tier` is copied as-is. If
  back-filled free users should get the Sub-E 30-day trial gesture instead,
  change `_TODO_TIER_POLICY` and add the reset (mirror `cutover_subproject_e`).
- **`email_conflict > 0`** means a legacy email already exists in Postgres
  under a different identity — reconcile those by hand (check the warning logs
  for the `legacy_id` / `email` pairs).
