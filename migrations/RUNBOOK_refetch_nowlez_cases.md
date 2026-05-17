# Runbook: Re-fetch Nowlez Cases (Sub-project A, Step 6.3)

This runbook walks the operator through the live execution of
`refetch_nowlez_cases.py` against staging Postgres, after Sub-project D's
cutover has already populated `_legacy_nowlez_client_cases` and
`_legacy_nowlez_case_orders`.

**Do not run any of these commands as part of automated CI.** They mutate
shared infrastructure (staging DB) and call the real eCourts API.

---

## 0. Pre-flight checks

Run all three checks before touching the live system. If any fails, **stop**
and consult the Sub-project A or D lead.

### 0.1 Legacy table is populated

```bash
psql -h staging-host -d nowlez_staging \
  -c "SELECT COUNT(*) FROM _legacy_nowlez_client_cases"
```

Expected: a positive integer (the Nowlez case row count post-D-cutover,
roughly equal to the production Nowlez user-case count). If the result is
`0`, Sub-project D's cutover has not yet materialised the legacy table on
this DB — abort and follow up with the D team.

#### Canonical legacy-table schema (contract with sub-project D)

The re-fetch script and its DAOs (`data_access.daos.case_dao.upsert_case`,
`data_access.daos.order_dao.get_legacy_orders_by_case`) expect EXACTLY
these column names. If sub-project D's cutover produces different names,
either rename in the cutover SQL or open a coordinated change with the A
team — do NOT silently ALTER the local DB to match.

```sql
CREATE TABLE _legacy_nowlez_client_cases (
    id              INTEGER PRIMARY KEY,
    user_id         UUID,
    cnr             TEXT,
    client_id       TEXT,
    refresh_enabled BOOLEAN,
    notes           TEXT
);

CREATE TABLE _legacy_nowlez_case_orders (
    id                   INTEGER PRIMARY KEY,
    client_case_id       INTEGER,         -- FK to _legacy_nowlez_client_cases.id
    order_id             TEXT,
    file_path            TEXT,
    page_count           INTEGER,
    preprocessed         BOOLEAN,
    preprocessed_at      TIMESTAMPTZ,
    retry_count          INTEGER,
    permanently_failed   BOOLEAN,
    uploaded_at          TIMESTAMPTZ
);
```

The column name `client_case_id` on the orders table is a historical
artefact of the legacy SQLite schema and is preserved as-is for
straight-copy compatibility. The DAO accepts a `legacy_case_id` parameter
on the Python side (semantic name) but emits `WHERE client_case_id = :cid`
in the raw SQL.

### 0.2 eCourts API is reachable from this host

```bash
python -c "from ecourts_client import fetch_case; \
  import asyncio; \
  r = asyncio.run(fetch_case('MHCC010054732024')); \
  print(r.cnr)"
```

Expected: prints `MHCC010054732024` (or whatever CNR you pick; just use a
real Bombay HC CNR). If this raises `CircuitOpen`, `CourtSiteDown`, or any
network error, the host can't talk to eCourts — fix that first (firewall,
DNS, JWT token) before running the migration.

### 0.3 Sub-project A migration is applied on staging

```bash
psql -h staging-host -d nowlez_staging -c "\d+ cases"
```

Expected: the table description prints with columns `id`, `user_id`, `cnr`,
`portal`, `parties (jsonb)`, etc. — i.e. the schema from
`data_access/alembic/versions/0002_add_case_tables.py`. If the table doesn't
exist, run `alembic upgrade head` (see Step 2) before proceeding.

---

## 1. Restore Nowlez DB snapshot to staging Postgres

```bash
pg_restore -h staging-host -U test -d nowlez_staging \
  C:\Project3\backups\nowlez_pre_cutover.dump
```

(Per Sub-project D's snapshot procedure — the dump file is whatever the D
team published as the pre-cutover Nowlez DB image.)

## 2. Apply Sub-project A's Alembic migration to staging

```bash
cd C:\Project3\shared\data-access
DATABASE_URL=postgresql://test@staging-host/nowlez_staging alembic upgrade head
```

This creates the `cases`, `case_orders`, and `case_orders_nowlez` tables.
Idempotent — safe to re-run if the schema is already at head.

## 3. Dry-run

```bash
cd C:\Project3\shared\migrations
DATABASE_URL=postgresql://test@staging-host/nowlez_staging \
  python refetch_nowlez_cases.py --dry-run
```

Expected output: `Would migrate N cases at concurrency=8` (no DB writes).
Confirms the script can read the legacy table and that the DB URL is wired
up correctly.

## 4. Live run on staging

```bash
cd C:\Project3\shared\migrations
DATABASE_URL=postgresql://test@staging-host/nowlez_staging \
  python refetch_nowlez_cases.py --concurrency=8
```

Expected output (after completion): a `Done: {...}` log line summarising
outcomes, e.g. `Done: {'migrated': 985, 'skipped': 0, 'cnr_not_found': 12, 'malformed': 3}`.

### 4.1 Time estimate

With `--concurrency=8` and a ~1.5 s average eCourts call latency:

- **~1000 cases**: ~20-30 minutes
- **~5000 cases**: ~1.5-2.5 hours

These are wall-clock estimates assuming eCourts is healthy. Real numbers
depend on the circuit breaker hit rate (every `CircuitOpen` falls into
`retry_later` and finishes near-instantly without doing useful work) and on
the eCourts site load at the time of run.

### 4.2 Monitoring during the run

- Start with `--concurrency=8`. Bump to `--concurrency=16` only if the
  eCourts circuit-breaker metrics in Sentry/Prometheus stay green and the
  health-monitor doesn't trip — otherwise the breaker will open mid-run.
- Tail Sentry for unexpected exceptions during the run. The script's
  `except Exception:` branch logs via `logger.exception(...)` and bumps the
  `error` counter; **non-zero `error` in the final dict means re-investigate
  before declaring success**.
- The script exits `2` if `error` > 0, `0` otherwise. CI/cron should treat
  non-zero as a hard failure.

### 4.3 What to do on failure mid-run

The script is idempotent: any row already present in `cases` (matched by
`(user_id, cnr)`) is short-circuited via the `case_dao.exists` check in
`migrate_one_case`. **Just re-run** with the same command.

The only failure modes that require operator intervention:

- **Transient eCourts outage** mid-run: the affected rows fall into
  `retry_later`. Re-run after eCourts recovers; previously-migrated rows
  are skipped, retry rows are re-attempted.
- **DB connection drop**: re-run. Already-flushed rows are skipped.
- **Disk full on staging Postgres**: free disk, then re-run.
- **Persistent `error` outcomes**: inspect Sentry breadcrumbs for the
  failing CNRs, fix the underlying issue (often parser bugs against
  unusual case shapes), then re-run.

## 5. Validate

```bash
cd C:\Project3\shared\migrations
DATABASE_URL=postgresql://test@staging-host/nowlez_staging \
  python refetch_nowlez_cases_validators.py
```

Expected output:

```
Legacy: <N>; New cases (any): <N - cnr_not_found_count>
Sample-diff problems: 0
FK integrity: OK
```

The legacy count should match the new count to within the
`cnr_not_found` + `malformed` tally from Step 4. Sample-diff > 0 or
`FK integrity` raising means the migration left holes — **do not promote
to production**.

---

## Rollback path

The re-fetch script writes ONLY to `cases`, `case_orders`, and
`case_orders_nowlez`. It does **NOT** touch:

- `_legacy_nowlez_client_cases` (source of truth — preserved)
- `_legacy_nowlez_case_orders` (source of truth — preserved)
- `users`, `users_nowlez`, `users_munshi` (identity — untouched)
- Any billing or whatsapp tables

So the safe rollback is:

```sql
-- Connect to the affected DB first; this is irreversible.
TRUNCATE cases CASCADE;
```

`TRUNCATE cases CASCADE` cascades through to `case_orders` (FK
`ON DELETE CASCADE` on `case_orders.case_id`) and `case_orders_nowlez`
(FK `ON DELETE CASCADE` on `case_orders_nowlez.order_id`), so a single
statement clears all three migrated tables. User data is preserved.

After the truncate, re-run Step 4 from a clean slate.

### Why not `alembic downgrade`?

Alembic downgrade also works and would `DROP TABLE` instead of
`TRUNCATE`, but it's more disruptive because the next upgrade has to
re-create the schema and indexes. Use `TRUNCATE CASCADE` for "I want to
re-try the migration"; reserve `alembic downgrade` for "I want to roll
the schema back to pre-A entirely".

---

## Production cutover note

This runbook targets **staging**. For the production cutover:

1. Coordinate with Sub-project D on the snapshot timing.
2. Take a fresh `pg_dump` of production before running anything (separate
   from the D-team snapshot).
3. Run Steps 0 -> 5 against the production DB during the agreed maintenance
   window.
4. Validate (Step 5) BEFORE flipping the read traffic.
5. If validation fails: rollback (see above), restore the pg_dump if
   needed, debug, and reschedule.
