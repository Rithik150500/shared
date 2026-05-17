"""Smoke tests for Case ORM model — does it instantiate, do columns exist."""
from data_access.models.case import Case, CaseOrder, CaseOrderNowlez


def test_case_columns_present():
    cols = {c.name for c in Case.__table__.columns}
    expected = {
        "id", "user_id", "cnr", "case_number", "title", "portal", "filing_year",
        "court", "judge", "stage", "case_status", "next_hearing_date",
        "refresh_enabled", "last_refreshed_at", "last_change_at", "notes",
        "client_id", "parties", "acts", "history", "fir", "objections", "category",
        "raw_response", "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_case_orders_columns_present():
    cols = {c.name for c in CaseOrder.__table__.columns}
    expected = {
        "id", "case_id", "order_id", "order_date", "descriptive_name",
        "order_url", "url_fetched_at", "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_case_orders_nowlez_columns_present():
    cols = {c.name for c in CaseOrderNowlez.__table__.columns}
    expected = {
        "order_id", "file_path", "file_storage", "page_count", "file_size_bytes",
        "preprocessed", "preprocessed_markdown_path", "preprocessed_at",
        "retry_count", "last_retry_at", "permanently_failed",
        "permanent_failure_reason", "uploaded_at", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"
