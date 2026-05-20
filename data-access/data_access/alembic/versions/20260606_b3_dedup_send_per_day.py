"""B-3: per-day dedup for whatsapp_delivery_log (audit fix).

Revision ID: 20260606_b3_dedup
Revises: 20260605_c_upsell
Create Date: 2026-05-20

Audit finding B-3 (Critical): the casepilot reminder crons
(``send_tomorrow_hearing_reminders`` / ``send_weekly_summaries``) gate
their once-per-day phase on a per-pod module global. Two pods running the
scheduler independently fire the daily phase, so users receive 2x sends
for ``nowlez_tomorrow_hearings_v1`` and ``nowlez_weekly_summary_v1``.

This migration adds the DB-side enforcement so duplicate daily sends are
impossible regardless of how many scheduler pods race:

1. Add ``send_date_ist DATE`` column (NULLABLE — transactional sends like
   ``nowlez_signup_welcome_v1`` and OTPs keep it NULL and bypass dedup).
2. Backfill existing rows from ``sent_at AT TIME ZONE 'Asia/Kolkata'``
   for rows that look like daily-cadence templates (template_name in a
   known allowlist). Other rows are left NULL.
3. De-duplicate any existing daily-cadence rows that already collide on
   the new key — keep the row with the smallest ``id`` (deterministic).
4. Add a PARTIAL unique index keyed on
   ``(user_id, template_name, send_date_ist) WHERE send_date_ist IS NOT NULL``
   so transactional rows with NULL ``send_date_ist`` are exempt.

The worker layer (``shared/whatsapp_delivery/dispatch/worker.py``) will
write rows with ``send_date_ist`` set on daily-cadence templates via an
``INSERT ... ON CONFLICT DO NOTHING`` claim; this DB constraint is the
serializable guard that survives a worker crash between claim and Meta
send (next-day cron retries; daily sends are inherently retry-tolerant).

down_revision points at the C-upsell migration (current head per
``alembic heads`` on 2026-05-20).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260606_b3_dedup"
down_revision = "20260605_c_upsell"
branch_labels = None
depends_on = None


# Templates that fire on a once-per-user-per-day cadence and benefit from
# the dedup index. Kept in sync with ``_DEDUP_DAILY_TEMPLATES`` in
# ``shared/whatsapp_delivery/dispatch/worker.py``. Transactional templates
# (signup welcome, OTP, order-uploaded, hearing-result, etc.) MUST NOT be
# listed here — they may legitimately fire multiple times per user per day.
_DAILY_CADENCE_TEMPLATES = (
    "nowlez_tomorrow_hearings_v1",
    "nowlez_weekly_summary_v1",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Add the nullable date column. Default NULL so existing rows and
    #    future transactional sends are unaffected.
    op.add_column(
        "whatsapp_delivery_log",
        sa.Column("send_date_ist", sa.Date(), nullable=True),
    )

    # 2. Backfill existing rows for the daily-cadence templates. Only those
    #    rows will participate in the new partial unique constraint; other
    #    rows stay NULL and are exempt.
    #
    #    Template names are inlined as quoted SQL literals (built from a
    #    Python-side allowlist of safe identifiers — no user input — and
    #    each name validated below) so the migration also renders cleanly
    #    in alembic's offline / SQL-emit mode where ``bindparam`` lists are
    #    not supported.
    for name in _DAILY_CADENCE_TEMPLATES:
        # Defensive: refuse to inline anything that isn't a bare identifier
        # so this never grows into an injection footgun later.
        if not name.replace("_", "").isalnum():
            raise RuntimeError(
                f"refusing to inline non-identifier template name: {name!r}"
            )
    names_sql = ", ".join(f"'{n}'" for n in _DAILY_CADENCE_TEMPLATES)
    if dialect == "postgresql":
        # AT TIME ZONE 'Asia/Kolkata' converts the TIMESTAMPTZ ``sent_at``
        # (or ``enqueued_at`` fallback when sent_at is NULL) to IST clock
        # time, then ::date strips the time component.
        op.execute(
            f"""
            UPDATE whatsapp_delivery_log
            SET send_date_ist = (COALESCE(sent_at, enqueued_at)
                                 AT TIME ZONE 'Asia/Kolkata')::date
            WHERE template_name IN ({names_sql})
            """
        )
    else:
        # SQLite path (unit tests): no timezone arithmetic — backfill from
        # the date part of the stored timestamp. Tests construct rows with
        # explicit timestamps so this is precise enough for assertions.
        op.execute(
            f"UPDATE whatsapp_delivery_log "
            f"SET send_date_ist = date(COALESCE(sent_at, enqueued_at)) "
            f"WHERE template_name IN ({names_sql})"
        )

    # 3. Deduplicate existing daily rows BEFORE we add the unique index, so
    #    the index creation cannot fail. Keep the row with the smallest id
    #    (deterministic and stable across re-runs).
    if dialect == "postgresql":
        op.execute(
            """
            DELETE FROM whatsapp_delivery_log w1
            USING whatsapp_delivery_log w2
            WHERE w1.user_id = w2.user_id
              AND w1.template_name = w2.template_name
              AND w1.send_date_ist = w2.send_date_ist
              AND w1.send_date_ist IS NOT NULL
              AND w1.id::text > w2.id::text
            """
        )
    else:
        # SQLite lacks the USING ... DELETE form. Equivalent: pick the
        # winner per (user_id, template_name, send_date_ist) group as the
        # row with the smallest id, delete the rest. ``id`` is a UUID stored
        # as String(36) on SQLite (with_variant), so text comparison is
        # well-defined.
        op.execute(
            """
            DELETE FROM whatsapp_delivery_log
            WHERE send_date_ist IS NOT NULL
              AND id NOT IN (
                SELECT MIN(id) FROM whatsapp_delivery_log
                WHERE send_date_ist IS NOT NULL
                GROUP BY user_id, template_name, send_date_ist
              )
            """
        )

    # 4. Add the partial unique index. Postgres syntax for partial unique
    #    is ``CREATE UNIQUE INDEX ... WHERE``; SQLite supports the same
    #    syntax since 3.8 so the index works on both targets.
    op.create_index(
        "whatsapp_delivery_log_user_template_day_unique",
        "whatsapp_delivery_log",
        ["user_id", "template_name", "send_date_ist"],
        unique=True,
        postgresql_where=sa.text("send_date_ist IS NOT NULL"),
        sqlite_where=sa.text("send_date_ist IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "whatsapp_delivery_log_user_template_day_unique",
        table_name="whatsapp_delivery_log",
    )
    op.drop_column("whatsapp_delivery_log", "send_date_ist")
