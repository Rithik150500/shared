"""Unified case-tracking models shared by Munshi and Nowlez."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base


# UUID type that survives the SQLite round-trip. Sub-project D's plain
# `UUID(...).with_variant(String(36), "sqlite")` only adjusts DDL — on read,
# SQLite hands back a str, and identity-map lookups against an in-memory
# UUID() instance miss (because str != UUID). This TypeDecorator coerces
# both directions so DAO callers always see uuid.UUID regardless of dialect.
class _UUIDType(TypeDecorator):
    impl = UUID(as_uuid=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(36))
        return dialect.type_descriptor(UUID(as_uuid=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return str(value) if isinstance(value, uuid.UUID) else value
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite" and isinstance(value, str):
            return uuid.UUID(value)
        return value


UUIDType = _UUIDType()
JSONBType = JSONB().with_variant(JSON(), "sqlite")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here because
        # SQLite errors on the literal at CREATE TABLE time. Prod schema is
        # preserved via the Alembic migration (op.create_table sets
        # server_default explicitly). Python-side default=uuid.uuid4 covers ORM
        # INSERTs on both Postgres and SQLite.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # cnr is eCourts-only now (nullable). Non-eCourts / manual forums have no
    # 16-char CNR — they are keyed by (user_id, forum, forum_case_ref) instead.
    cnr: Mapped[str | None] = mapped_column(String(16), nullable=True)
    case_number: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)

    # Multi-forum discriminator. eCourts rows use ecourts_district /
    # ecourts_highcourt (kept consistent with `portal` via a CHECK); SC /
    # consumer / drt / arbitration have no `portal`.
    forum: Mapped[str] = mapped_column(
        Text, nullable=False, default="ecourts_district",
        server_default=text("'ecourts_district'"),
    )
    # Universal per-forum identity: == cnr for eCourts; the user's normalized
    # case number for other forums; a synthetic 'm-<uuid4hex>' for a manual case
    # with no official number. Unique per (user_id, forum).
    forum_case_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance / whether an automated adapter refreshes this row. Manual rows
    # ('manual') are skipped by get_due_for_refresh regardless of refresh_enabled.
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="ecourts_auto",
        server_default=text("'ecourts_auto'"),
    )

    # eCourts sub-classifier (district/highcourt). NULL for non-eCourts forums.
    portal: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tribunal sub-classifier (nclt/nclat/cat/itat/…). Set IFF forum='tribunal';
    # NULL for every other forum. The structural analog of `portal` for the
    # generic tribunal family — read hot by capability + refresh routing, and
    # part of the tribunal uniqueness key (see __table_args__).
    tribunal_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_year: Mapped[int | None] = mapped_column(Integer)

    court: Mapped[str | None] = mapped_column(Text)
    judge: Mapped[str | None] = mapped_column(Text)

    stage: Mapped[str | None] = mapped_column(Text)
    case_status: Mapped[str | None] = mapped_column(Text)
    next_hearing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # B.5b: timestamp of the first NDOH email dispatched for this case. NULL
    # means "no first-NDOH email has been sent yet" — set by the Nowlez E2
    # hook on first-send, never cleared. Replaces the SQLite-side flag the
    # Nowlez bot used to gate one-shot E2 sends across restarts.
    first_ndoh_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True,
    )

    # JSONB columns: Python-side defaults so SQLite INSERTs work without the
    # Postgres `'[]'::jsonb` server_default literal.
    parties: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    acts: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    history: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    fir: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)
    objections: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)
    category: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)

    raw_response: Mapped[dict[str, Any]] = mapped_column(
        JSONBType, nullable=False, default=dict
    )

    # --- Step-3: detail blobs migrated off disk into PG (D1) ---
    # case_detail_json is JSONB on PG (JSON on the SQLite test variant) so the
    # native calendar/timeline rewrite can query history/next_hearing without a
    # second json.loads; the shim json.dumps()-es it back to the legacy string
    # CaseRecord.case_detail_json. md/mini are plain TEXT.
    case_detail_json: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    case_detail_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    mini_case_detail_md: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    orders: Mapped[list["CaseOrder"]] = relationship(
        "CaseOrder", back_populates="case", cascade="all, delete-orphan",
    )

    __table_args__ = (
        # eCourts uniqueness kept as a PARTIAL unique index so multiple non-eCourts
        # rows (cnr IS NULL) never collide. Postgres/SQLite both treat NULLs as
        # distinct in a unique index; the partial predicate makes intent explicit
        # and keeps the index small. (Was a table UniqueConstraint pre-multiforum.)
        Index(
            "cases_user_cnr_unique", "user_id", "cnr", unique=True,
            postgresql_where=text("cnr IS NOT NULL"),
        ),
        # Universal per-forum identity uniqueness, SPLIT so a shared 'tribunal'
        # forum (many kinds) can't weaken eCourts/consumer collision detection:
        #   - non-tribunal rows key on (user, forum, ref)   [tribunal_kind IS NULL]
        #   - tribunal rows key on    (user, forum, kind, ref) [tribunal_kind NOT NULL]
        # Both dialect predicates set so the split holds on the SQLite test DB too
        # (without sqlite_where, SQLite would index tribunal rows in the NULL index
        # and collide two kinds sharing a ref).
        Index(
            "cases_user_forum_ref_unique", "user_id", "forum", "forum_case_ref",
            unique=True,
            postgresql_where=text("tribunal_kind IS NULL"),
            sqlite_where=text("tribunal_kind IS NULL"),
        ),
        Index(
            "cases_user_tribunal_ref_unique",
            "user_id", "forum", "tribunal_kind", "forum_case_ref", unique=True,
            postgresql_where=text("tribunal_kind IS NOT NULL"),
            sqlite_where=text("tribunal_kind IS NOT NULL"),
        ),
        CheckConstraint(
            "portal IS NULL OR portal IN ('district', 'highcourt')",
            name="cases_portal_check",
        ),
        CheckConstraint(
            "forum IN ('ecourts_district', 'ecourts_highcourt', 'supreme_court', "
            "'consumer', 'drt', 'arbitration', 'tribunal')",
            name="cases_forum_check",
        ),
        CheckConstraint(
            "source IN ('ecourts_auto', 'manual', 'ejagriti_auto', 'drt_auto', "
            "'sc_auto', 'tribunal_auto')",
            name="cases_source_check",
        ),
        # eCourts forum and portal must stay consistent; non-eCourts forums ignore portal.
        CheckConstraint(
            "(forum = 'ecourts_district'  AND portal = 'district')  OR "
            "(forum = 'ecourts_highcourt' AND portal = 'highcourt') OR "
            "(forum NOT IN ('ecourts_district', 'ecourts_highcourt'))",
            name="cases_forum_portal_consistency",
        ),
        # tribunal_kind is set IFF the forum is the generic tribunal forum.
        CheckConstraint(
            "(forum = 'tribunal' AND tribunal_kind IS NOT NULL) OR "
            "(forum <> 'tribunal' AND tribunal_kind IS NULL)",
            name="cases_tribunal_kind_consistency",
        ),
        Index("cases_user_id_idx", "user_id"),
        Index(
            "cases_next_hearing_date_idx", "next_hearing_date",
            postgresql_where=text("next_hearing_date IS NOT NULL"),
        ),
        Index(
            "cases_last_change_at_idx", text("last_change_at DESC"),
            postgresql_where=text("last_change_at IS NOT NULL"),
        ),
        Index(
            "cases_refresh_queue_idx", "refresh_enabled", "last_refreshed_at",
            postgresql_where=text("refresh_enabled IS TRUE"),
        ),
        Index(
            "cases_client_id_idx", "client_id",
            postgresql_where=text("client_id IS NOT NULL"),
        ),
        Index("cases_cnr_idx", "cnr"),
    )


class CaseOrder(Base):
    __tablename__ = "case_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    descriptive_name: Mapped[str | None] = mapped_column(Text)
    order_url: Mapped[str | None] = mapped_column(Text)
    url_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    case: Mapped["Case"] = relationship("Case", back_populates="orders")
    nowlez_ext: Mapped["CaseOrderNowlez | None"] = relationship(
        "CaseOrderNowlez", back_populates="order", cascade="all, delete-orphan", uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("case_id", "order_id", name="case_orders_case_order_unique"),
        Index("case_orders_case_id_idx", "case_id"),
        Index("case_orders_order_date_idx", "case_id", text("order_date DESC")),
    )


class CaseOrderNowlez(Base):
    __tablename__ = "case_orders_nowlez"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("case_orders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_path: Mapped[str | None] = mapped_column(Text)
    file_storage: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    preprocessed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    preprocessed_markdown_path: Mapped[str | None] = mapped_column(Text)
    preprocessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permanently_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    permanent_failure_reason: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # B.5b: timestamp of the E3 "order user-notified" WhatsApp message. NULL
    # means "user has not yet been told about this order". The Nowlez hook
    # picks unnotified orders via `WHERE user_notified_at IS NULL` so the
    # default NULL on new rows correctly enqueues each order exactly once.
    user_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    order: Mapped["CaseOrder"] = relationship("CaseOrder", back_populates="nowlez_ext")

    __table_args__ = (
        CheckConstraint(
            "file_storage IS NULL OR file_storage IN ('r2', 'local')",
            name="case_orders_nowlez_storage_check",
        ),
        Index(
            "case_orders_nowlez_preprocess_queue_idx",
            "preprocessed", "retry_count", "last_retry_at",
            postgresql_where=text(
                "preprocessed IS FALSE AND permanently_failed IS FALSE"
            ),
        ),
    )


@event.listens_for(Case, "before_insert")
def _fill_ecourts_identity(mapper, connection, target: "Case") -> None:
    """Backfill eCourts identity for rows built with just a cnr.

    ``forum_case_ref`` is NOT NULL and ``forum`` must match ``portal`` (CHECK).
    Rather than make every eCourts construction site repeat that boilerplate,
    derive them here for any row that carries a CNR: align ``forum`` to
    ``portal`` and default ``forum_case_ref`` to the CNR. Manual / non-eCourts
    rows have ``cnr IS NULL`` and set their own forum/forum_case_ref/source via
    ``case_dao.create_manual_case``, so this no-ops for them.
    """
    if target.cnr is None:
        return
    if target.portal in ("district", "highcourt"):
        target.forum = (
            "ecourts_highcourt" if target.portal == "highcourt"
            else "ecourts_district"
        )
    if not target.forum_case_ref:
        target.forum_case_ref = target.cnr
