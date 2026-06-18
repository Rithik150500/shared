"""TDD tests for whatsapp_delivery.tools.broadcast_send.

Covers:
- select_recipients (pure function — no DB, no network)
- send-loop orchestration (DB via in-memory SQLite, Meta calls mocked)
- Daily-cap guard
- Dry-run short-circuit
- Retry / failure paths

Session semantics note: broadcast_send uses a fresh get_session() per
recipient to ensure per-send durability (a crash mid-run leaves already-
sent rows committed). Tests use the db_session fixture from data-access/tests
conftest ported inline here so there's no cross-package conftest dependency.
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import data_access.models  # noqa — register all models so create_all works
from data_access.base import Base
from data_access.daos import broadcast_dao
from whatsapp_delivery.errors import MetaInvalidMessage, MetaTransientError
from whatsapp_delivery.tools.broadcast_send import Recipient, select_recipients

# ---------------------------------------------------------------------------
# SQLite in-memory session fixture (mirrors data-access conftest)
# ---------------------------------------------------------------------------

sqlite3.register_adapter(uuid.UUID, str)


@pytest.fixture()
def db_session():
    """Per-test SQLite in-memory session."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def sqlite_session_factory():
    """Returns a callable that creates a fresh SQLite session each call.

    Used by the send-loop tests that patch get_session() to yield sessions
    backed by a shared in-memory SQLite database so DAO state persists
    across the multiple get_session() calls the driver makes.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    from contextlib import contextmanager

    @contextmanager
    def _session():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    yield _session
    engine.dispose()


# ---------------------------------------------------------------------------
# Helpers — build minimal row dicts
# ---------------------------------------------------------------------------


def _row(wa_digits: str, name: str = "Test User", tier: str = "T1 Premium") -> dict:
    return {"WA_Digits": wa_digits, "Name (clean)": name, "Tier label": tier}


# ---------------------------------------------------------------------------
# select_recipients — pure function tests
# ---------------------------------------------------------------------------


class TestSelectRecipients:
    """Pure function; no DB or network."""

    def test_tier_filter_keeps_matching_tier(self):
        rows = [
            _row("91001", tier="T1 Premium"),
            _row("91002", tier="T2 Standard"),
            _row("91003", tier="T1 Basic"),
        ]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        digits = [r.wa_digits for r in result]
        assert "91001" in digits
        assert "91003" in digits
        assert "91002" not in digits

    def test_tier_filter_t2(self):
        rows = [
            _row("91001", tier="T1 Alpha"),
            _row("91002", tier="T2 Beta"),
        ]
        result = select_recipients(
            rows, tier="T2", suppressed=set(), already_done=set(), limit=100
        )
        assert [r.wa_digits for r in result] == ["91002"]

    def test_excludes_suppressed(self):
        rows = [_row("91001"), _row("91002")]
        result = select_recipients(
            rows, tier="T1", suppressed={"91001"}, already_done=set(), limit=100
        )
        assert [r.wa_digits for r in result] == ["91002"]

    def test_excludes_already_done(self):
        rows = [_row("91001"), _row("91002")]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done={"91001"}, limit=100
        )
        assert [r.wa_digits for r in result] == ["91002"]

    def test_excludes_both_suppressed_and_already_done(self):
        rows = [_row("91001"), _row("91002"), _row("91003")]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed={"91001"},
            already_done={"91002"},
            limit=100,
        )
        assert [r.wa_digits for r in result] == ["91003"]

    def test_blank_wa_digits_skipped(self):
        rows = [
            {"WA_Digits": "", "Name (clean)": "Nobody", "Tier label": "T1 Foo"},
            {"WA_Digits": None, "Name (clean)": "Nobody2", "Tier label": "T1 Foo"},
            _row("91001"),
        ]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        assert len(result) == 1
        assert result[0].wa_digits == "91001"

    def test_blank_name_falls_back_to_name_fallback(self):
        rows = [
            {"WA_Digits": "91001", "Name (clean)": "", "Tier label": "T1 Foo"},
            {"WA_Digits": "91002", "Name (clean)": None, "Tier label": "T1 Foo"},
        ]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed=set(),
            already_done=set(),
            limit=100,
            name_fallback="friend",
        )
        assert all(r.name == "friend" for r in result)

    def test_name_fallback_default_is_there(self):
        rows = [{"WA_Digits": "91001", "Name (clean)": "", "Tier label": "T1 Foo"}]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        assert result[0].name == "there"

    def test_pandas_nan_name_falls_back(self):
        # pandas reads a blank cell as float('nan'), which is TRUTHY and whose
        # str() is the literal "nan" — must NOT become "Hi nan,".
        rows = [
            {"WA_Digits": "91001", "Name (clean)": float("nan"), "Tier label": "T2 X"},
            {"WA_Digits": "91002", "Name (clean)": "nan", "Tier label": "T2 X"},
            {"WA_Digits": "91003", "Name (clean)": "NaN", "Tier label": "T2 X"},
        ]
        result = select_recipients(
            rows, tier="T2", suppressed=set(), already_done=set(), limit=100
        )
        assert [r.name for r in result] == ["there", "there", "there"]

    def test_clean_name_used_when_present(self):
        rows = [_row("91001", name="Rahul Kumar")]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        assert result[0].name == "Rahul Kumar"

    def test_limit_caps_output(self):
        rows = [_row(f"9100{i}") for i in range(10)]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=3
        )
        assert len(result) == 3

    def test_limit_of_zero_returns_empty(self):
        rows = [_row("91001")]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=0
        )
        assert result == []

    def test_empty_rows_returns_empty(self):
        result = select_recipients(
            [], tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        assert result == []

    def test_recipient_is_dataclass(self):
        rows = [_row("91001", name="Alice")]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        r = result[0]
        assert isinstance(r, Recipient)
        assert r.wa_digits == "91001"
        assert r.name == "Alice"

    def test_whitespace_name_falls_back(self):
        rows = [{"WA_Digits": "91001", "Name (clean)": "   ", "Tier label": "T1 Foo"}]
        result = select_recipients(
            rows, tier="T1", suppressed=set(), already_done=set(), limit=100
        )
        assert result[0].name == "there"


# ---------------------------------------------------------------------------
# Send-loop tests — use real DAO on SQLite, mock Meta I/O
# ---------------------------------------------------------------------------


def _make_args(
    *,
    tier: str = "T1",
    per_run_cap: int = 100,
    daily_cap: int = 200,
    dry_run: bool = False,
    yes: bool = True,
    template: str = "munshi_welcome_video_v1",
    language: str = "en",
    campaign: str = "test_campaign",
    video_media_id: str = "MEDIA_ID_123",
    video_file: str | None = None,
    spacing_seconds: float = 0.0,
    retry_backoff: float = 0.0,
    xlsx: str = "fake.xlsx",
    phone_number_id: str = "TEST_PHONE_ID",
    access_token: str = "TEST_TOKEN",
    # Auto-pause guard params (defaults keep existing tests unaffected)
    max_fail_rate: float = 0.99,
    max_undeliverable: float = 0.99,
    min_sample: int = 999_999,
) -> SimpleNamespace:
    return SimpleNamespace(
        tier=tier,
        per_run_cap=per_run_cap,
        daily_cap=daily_cap,
        dry_run=dry_run,
        yes=yes,
        template=template,
        language=language,
        campaign=campaign,
        video_media_id=video_media_id,
        video_file=video_file,
        spacing_seconds=spacing_seconds,
        retry_backoff=retry_backoff,
        xlsx=xlsx,
        phone_number_id=phone_number_id,
        access_token=access_token,
        max_fail_rate=max_fail_rate,
        max_undeliverable=max_undeliverable,
        min_sample=min_sample,
    )


def _recipients(*digits: str) -> list[Recipient]:
    return [Recipient(wa_digits=d, name=f"User {d[-4:]}") for d in digits]


class TestSendLoop:
    """Tests for run() — use real DAO on SQLite in-memory, mock Meta calls."""

    def _patch_get_session(self, monkeypatch, factory):
        """Patch data_access.engine.get_session and broadcast_send's import of it."""
        monkeypatch.setattr(
            "whatsapp_delivery.tools.broadcast_send.get_session", factory
        )

    def test_suppressed_recipients_skipped(self, monkeypatch, sqlite_session_factory):
        """A suppressed number must not appear in recipients passed to send loop."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002")]
        args = _make_args()

        # Pre-suppress 91001 in the DB
        with sqlite_session_factory() as s:
            broadcast_dao.suppress(s, wa_digits="91001", reason="stop")

        send_mock = MagicMock(return_value="wamid.abc")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch(
            "whatsapp_delivery.tools.broadcast_send.TemplateClient"
        ) as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        # Only 91002 should have been sent
        calls_to_digits = [
            c.kwargs["to"] for c in send_mock.call_args_list
        ]
        assert "91001" not in calls_to_digits
        assert "91002" in calls_to_digits

    def test_already_done_recipients_skipped(
        self, monkeypatch, sqlite_session_factory
    ):
        """A recipient already in the broadcast ledger must be skipped."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002")]
        args = _make_args()

        # Pre-claim 91001 so already_done_set sees it
        with sqlite_session_factory() as s:
            broadcast_dao.claim_send(
                s,
                campaign=args.campaign,
                wa_digits="91001",
                tier="T1",
                template_name=args.template,
                language=args.language,
            )
            broadcast_dao.mark_sent(s, campaign=args.campaign, wa_digits="91001", wamid="wamid.old")

        send_mock = MagicMock(return_value="wamid.new")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        calls_to_digits = [c.kwargs["to"] for c in send_mock.call_args_list]
        assert "91001" not in calls_to_digits
        assert "91002" in calls_to_digits

    def test_claim_send_called_before_send(self, monkeypatch, sqlite_session_factory):
        """claim_send must be called and must return True before each send."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args()

        call_order = []

        original_claim = broadcast_dao.claim_send

        def tracking_claim(s, **kw):
            result = original_claim(s, **kw)
            call_order.append(("claim", kw["wa_digits"], result))
            return result

        send_mock = MagicMock(return_value="wamid.x")

        def tracking_send(**kw):
            call_order.append(("send", kw["to"]))
            return "wamid.x"

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        import whatsapp_delivery.tools.broadcast_send as _mod
        monkeypatch.setattr(_mod._dao, "claim_send", tracking_claim)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = tracking_send
            run(args, rows=rows)

        assert len(call_order) == 2
        assert call_order[0] == ("claim", "91001", True)
        assert call_order[1] == ("send", "91001")

    def test_mark_sent_called_with_wamid_on_success(
        self, monkeypatch, sqlite_session_factory
    ):
        """After a successful send, mark_sent must be recorded in the DB."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args()

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock(
                return_value="wamid.success123"
            )
            run(args, rows=rows)

        # Verify DB state
        with sqlite_session_factory() as s:
            done = broadcast_dao.already_done_set(s, args.campaign)
            assert "91001" in done
            # Fetch the row to confirm wamid
            from data_access.models.broadcast import WaBroadcastLog
            from sqlalchemy import select

            row = s.execute(
                select(WaBroadcastLog).where(
                    WaBroadcastLog.campaign == args.campaign,
                    WaBroadcastLog.wa_digits == "91001",
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.meta_message_id == "wamid.success123"
            assert row.status == "sent"

    def test_daily_cap_trims_recipients(self, monkeypatch, sqlite_session_factory):
        """If sent_24h + len(recipients) > daily_cap, trim to fit."""
        from whatsapp_delivery.tools.broadcast_send import run

        # Pre-fill 3 sent rows in the DB for this campaign
        with sqlite_session_factory() as s:
            for i in range(3):
                broadcast_dao.claim_send(
                    s,
                    campaign="test_campaign",
                    wa_digits=f"9100{i}",
                    tier="T1",
                    template_name="munshi_welcome_video_v1",
                    language="en",
                )
                broadcast_dao.mark_sent(
                    s,
                    campaign="test_campaign",
                    wa_digits=f"9100{i}",
                    wamid=f"wamid.old{i}",
                )

        # daily_cap=5, sent_24h=3, new_candidates=4 → only 2 should be sent
        rows = [_row(f"9200{i}") for i in range(4)]  # 4 new candidates
        args = _make_args(daily_cap=5, per_run_cap=100)

        send_mock = MagicMock(return_value="wamid.new")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        assert send_mock.call_count == 2

    def test_daily_cap_zero_remaining_sends_nothing(
        self, monkeypatch, sqlite_session_factory
    ):
        """If daily_cap is already exhausted, no sends occur."""
        from whatsapp_delivery.tools.broadcast_send import run

        with sqlite_session_factory() as s:
            for i in range(5):
                broadcast_dao.claim_send(
                    s,
                    campaign="test_campaign",
                    wa_digits=f"9100{i}",
                    tier="T1",
                    template_name="munshi_welcome_video_v1",
                    language="en",
                )
                broadcast_dao.mark_sent(
                    s,
                    campaign="test_campaign",
                    wa_digits=f"9100{i}",
                    wamid=f"wamid.old{i}",
                )

        rows = [_row("91999")]
        args = _make_args(daily_cap=5)

        send_mock = MagicMock(return_value="wamid.new")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        send_mock.assert_not_called()

    def test_transient_error_retried_3x_then_mark_failed(
        self, monkeypatch, sqlite_session_factory
    ):
        """MetaTransientError on all 3 attempts → mark_failed_local in DB."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(retry_backoff=0.0)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock(
                side_effect=MetaTransientError("server error")
            )
            run(args, rows=rows)

        with sqlite_session_factory() as s:
            from data_access.models.broadcast import WaBroadcastLog
            from sqlalchemy import select

            row = s.execute(
                select(WaBroadcastLog).where(
                    WaBroadcastLog.campaign == args.campaign,
                    WaBroadcastLog.wa_digits == "91001",
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.status == "failed"
            assert row.failure_reason is not None

    def test_transient_error_retries_exactly_3_times(
        self, monkeypatch, sqlite_session_factory
    ):
        """send_template_with_components must be called exactly 3 times before giving up."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(retry_backoff=0.0)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(side_effect=MetaTransientError("boom"))

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        assert send_mock.call_count == 3

    def test_transient_error_succeeds_on_second_attempt(
        self, monkeypatch, sqlite_session_factory
    ):
        """If attempt 0 fails transiently but attempt 1 succeeds, status=sent."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(retry_backoff=0.0)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(
            side_effect=[MetaTransientError("first fail"), "wamid.retry_ok"]
        )

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        assert send_mock.call_count == 2

        with sqlite_session_factory() as s:
            from data_access.models.broadcast import WaBroadcastLog
            from sqlalchemy import select

            row = s.execute(
                select(WaBroadcastLog).where(
                    WaBroadcastLog.campaign == args.campaign,
                    WaBroadcastLog.wa_digits == "91001",
                )
            ).scalar_one_or_none()
            assert row.status == "sent"
            assert row.meta_message_id == "wamid.retry_ok"

    def test_invalid_message_immediate_mark_failed(
        self, monkeypatch, sqlite_session_factory
    ):
        """MetaInvalidMessage → mark_failed immediately, no retry."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args()

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(side_effect=MetaInvalidMessage("bad number"))

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        # Only 1 attempt (no retry on invalid)
        assert send_mock.call_count == 1

        with sqlite_session_factory() as s:
            from data_access.models.broadcast import WaBroadcastLog
            from sqlalchemy import select

            row = s.execute(
                select(WaBroadcastLog).where(
                    WaBroadcastLog.campaign == args.campaign,
                    WaBroadcastLog.wa_digits == "91001",
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.status == "failed"

    def test_dry_run_no_sends_no_upload(self, monkeypatch, sqlite_session_factory):
        """Dry-run must not call send_template_with_components or upload_media."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002")]
        args = _make_args(dry_run=True, yes=False, video_media_id=None, video_file="fake.mp4")

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(return_value="wamid.x")
        upload_mock = MagicMock(return_value="MEDIA_ID_DRY")

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            run(args, rows=rows)

        send_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_video_media_id_supplied_skips_upload(
        self, monkeypatch, sqlite_session_factory
    ):
        """When --video-media-id is provided, upload_media must NOT be called."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(video_media_id="PREMADE_ID", video_file=None)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        upload_mock = MagicMock(return_value="SHOULD_NOT_BE_USED")
        send_mock = MagicMock(return_value="wamid.ok")

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            run(args, rows=rows)

        upload_mock.assert_not_called()
        # send was called with the pre-made media id
        assert send_mock.call_args.kwargs["header_video_id"] == "PREMADE_ID"

    def test_video_file_triggers_upload(
        self, monkeypatch, sqlite_session_factory, tmp_path
    ):
        """When --video-file is supplied (no --video-media-id), upload_media is called once."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002")]
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"fake-video-bytes")
        args = _make_args(video_media_id=None, video_file=str(video_path))

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        upload_mock = MagicMock(return_value="UPLOADED_ID")
        send_mock = MagicMock(return_value="wamid.ok")

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            run(args, rows=rows)

        upload_mock.assert_called_once()
        # Both sends must use the uploaded media id
        for c in send_mock.call_args_list:
            assert c.kwargs["header_video_id"] == "UPLOADED_ID"

    def test_race_skip_when_claim_returns_false(
        self, monkeypatch, sqlite_session_factory
    ):
        """If claim_send returns False (concurrent send), the recipient is skipped."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args()

        # Pre-claim so claim_send returns False
        with sqlite_session_factory() as s:
            broadcast_dao.claim_send(
                s,
                campaign=args.campaign,
                wa_digits="91001",
                tier="T1",
                template_name=args.template,
                language=args.language,
            )

        send_mock = MagicMock(return_value="wamid.x")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            run(args, rows=rows)

        # claim_send returned False → already in done set → filtered by select_recipients
        # (or if claim is checked again mid-loop, still skipped)
        send_mock.assert_not_called()

    def test_spacing_sleep_called_per_recipient(
        self, monkeypatch, sqlite_session_factory
    ):
        """time.sleep(spacing_seconds) must be called after each recipient's send."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002"), _row("91003")]
        args = _make_args(spacing_seconds=1.5)

        sleep_calls = []
        monkeypatch.setattr("time.sleep", lambda t: sleep_calls.append(t))
        self._patch_get_session(monkeypatch, sqlite_session_factory)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock(
                return_value="wamid.ok"
            )
            run(args, rows=rows)

        # Spacing sleep after each recipient (retry sleeps won't fire since no errors)
        spacing_sleeps = [t for t in sleep_calls if t == 1.5]
        assert len(spacing_sleeps) == 3

    def test_run_returns_zero_exit_code_on_success(
        self, monkeypatch, sqlite_session_factory
    ):
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args()

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock(
                return_value="wamid.ok"
            )
            exit_code = run(args, rows=rows)

        assert exit_code == 0

    def test_run_dry_run_returns_zero_exit_code(
        self, monkeypatch, sqlite_session_factory
    ):
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(dry_run=True, yes=False)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock()
            exit_code = run(args, rows=rows)

        assert exit_code == 0


# ---------------------------------------------------------------------------
# CLI / argparse smoke-test
# ---------------------------------------------------------------------------


class TestCLI:
    def test_main_dry_run_no_yes_succeeds(self, monkeypatch, tmp_path):
        """main() in dry-run mode must not crash (no xlsx reads, no network)."""
        from openpyxl import Workbook

        from whatsapp_delivery.tools.broadcast_send import main

        # Write a minimal xlsx so _load_rows can read it
        xlsx_path = tmp_path / "broadcast.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Broadcast List"
        ws.append(["WA_Digits", "Name (clean)", "Tier label"])
        ws.append(["91001", "Alice", "T1 Premium"])
        ws.append(["91002", "Bob", "T2 Standard"])
        wb.save(xlsx_path)

        monkeypatch.setenv("META_PHONE_NUMBER_ID", "TEST_PHONE_ID")
        monkeypatch.setenv("META_ACCESS_TOKEN", "TEST_TOKEN")
        monkeypatch.setenv("META_VERIFY_TOKEN", "TEST_VERIFY")
        monkeypatch.setenv("META_APP_SECRET", "TEST_SECRET")
        monkeypatch.setenv("SHARED_REDIS_URL", "redis://localhost:6379/0")

        # Patch get_session so no real DB is needed
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        from contextlib import contextmanager

        @contextmanager
        def _fake_session():
            s = Session()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(
            "whatsapp_delivery.tools.broadcast_send.get_session", _fake_session
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        exit_code = main(
            [
                "--xlsx", str(xlsx_path),
                "--campaign", "cli_test",
                "--tier", "T1",
                "--dry-run",
            ]
        )
        assert exit_code == 0


# ---------------------------------------------------------------------------
# _load_rows — openpyxl-based loader unit tests
# ---------------------------------------------------------------------------


class TestLoadRows:
    """Tests for _load_rows: reads 'Broadcast List' sheet via openpyxl.

    Verifies:
    - blank name cell → "" (NOT "nan")
    - integer phone → clean digit string (no ".0" suffix)
    - dict keys match the header row exactly
    """

    def _write_xlsx(self, path, rows: list[list]) -> None:
        """Write a workbook with a 'Broadcast List' sheet from header + data rows."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Broadcast List"
        for r in rows:
            ws.append(r)
        wb.save(path)

    def test_blank_name_cell_returns_empty_string(self, tmp_path):
        """A blank Name (clean) cell must come back as '' (not 'nan' or 'None')."""
        from whatsapp_delivery.tools.broadcast_send import _load_rows

        xlsx_path = tmp_path / "test.xlsx"
        self._write_xlsx(xlsx_path, [
            ["WA_Digits", "Name (clean)", "Tier label"],
            [916001141186, None, "T1 Premium"],  # blank name
            ["916002000000", "Alice", "T2 Standard"],
        ])

        rows = _load_rows(str(xlsx_path))

        assert len(rows) == 2
        assert rows[0]["Name (clean)"] == "", (
            f"Blank cell must be '' but got {rows[0]['Name (clean)']!r}"
        )

    def test_integer_phone_no_dot_zero(self, tmp_path):
        """An integer-valued phone cell (e.g. 916001141186) must stringify to '916001141186'."""
        from whatsapp_delivery.tools.broadcast_send import _load_rows

        xlsx_path = tmp_path / "test.xlsx"
        self._write_xlsx(xlsx_path, [
            ["WA_Digits", "Name (clean)", "Tier label"],
            [916001141186, "Rahul", "T1 Premium"],
        ])

        rows = _load_rows(str(xlsx_path))

        assert len(rows) == 1
        phone = rows[0]["WA_Digits"]
        assert phone == "916001141186", (
            f"Expected '916001141186' but got {phone!r} — openpyxl int must not gain '.0'"
        )
        assert "." not in phone, "Phone string must not contain a decimal point"

    def test_dict_keys_match_headers(self, tmp_path):
        """Keys of each returned dict must exactly match the header row."""
        from whatsapp_delivery.tools.broadcast_send import _load_rows

        xlsx_path = tmp_path / "test.xlsx"
        headers = ["WA_Digits", "Name (clean)", "Tier label"]
        self._write_xlsx(xlsx_path, [
            headers,
            ["91001", "Bob", "T1 Premium"],
        ])

        rows = _load_rows(str(xlsx_path))

        assert len(rows) == 1
        assert set(rows[0].keys()) == set(headers)

    def test_blank_name_and_integer_phone_together(self, tmp_path):
        """Combined: integer phone + blank name + normal row — all three columns correct."""
        from whatsapp_delivery.tools.broadcast_send import _load_rows

        xlsx_path = tmp_path / "test.xlsx"
        self._write_xlsx(xlsx_path, [
            ["WA_Digits", "Name (clean)", "Tier label"],
            [916001141186, None, "T1 Alpha"],   # integer phone + blank name
            ["916002000000", "Alice", "T2 Beta"],  # string phone + string name
        ])

        rows = _load_rows(str(xlsx_path))

        assert len(rows) == 2
        # Row 0: integer phone → clean string, blank name → ""
        assert rows[0]["WA_Digits"] == "916001141186"
        assert rows[0]["Name (clean)"] == ""
        assert rows[0]["Tier label"] == "T1 Alpha"
        # Row 1: string values pass through unchanged
        assert rows[1]["WA_Digits"] == "916002000000"
        assert rows[1]["Name (clean)"] == "Alice"


# ---------------------------------------------------------------------------
# Auto-pause guard tests (Task 8)
# ---------------------------------------------------------------------------


def _seed_ledger(
    factory,
    *,
    campaign: str,
    tier: str = "T1",
    n_failed: int = 0,
    n_sent: int = 0,
    n_delivered: int = 0,
    n_read: int = 0,
    fail_error_code: int | None = None,
) -> None:
    """Seed the in-memory ledger with a synthetic outcome history for guard tests.

    All digits are generated deterministically as 5-digit strings starting from
    ``10000`` so they never collide with rows created by the send loop.
    """
    counter = 10000
    with factory() as s:
        for _ in range(n_sent):
            wa = str(counter); counter += 1
            broadcast_dao.claim_send(
                s, campaign=campaign, wa_digits=wa, tier=tier,
                template_name="munshi_welcome_video_v1", language="en",
            )
            broadcast_dao.mark_sent(s, campaign=campaign, wa_digits=wa, wamid=f"wamid.s{wa}")
        for _ in range(n_delivered):
            wa = str(counter); counter += 1
            broadcast_dao.claim_send(
                s, campaign=campaign, wa_digits=wa, tier=tier,
                template_name="munshi_welcome_video_v1", language="en",
            )
            broadcast_dao.mark_sent(s, campaign=campaign, wa_digits=wa, wamid=f"wamid.d{wa}")
            # Promote to delivered via apply_broadcast_status
            broadcast_dao.apply_broadcast_status(
                s, wamid=f"wamid.d{wa}", status="delivered",
            )
        for _ in range(n_read):
            wa = str(counter); counter += 1
            broadcast_dao.claim_send(
                s, campaign=campaign, wa_digits=wa, tier=tier,
                template_name="munshi_welcome_video_v1", language="en",
            )
            broadcast_dao.mark_sent(s, campaign=campaign, wa_digits=wa, wamid=f"wamid.r{wa}")
            broadcast_dao.apply_broadcast_status(
                s, wamid=f"wamid.r{wa}", status="read",
            )
        for _ in range(n_failed):
            wa = str(counter); counter += 1
            broadcast_dao.claim_send(
                s, campaign=campaign, wa_digits=wa, tier=tier,
                template_name="munshi_welcome_video_v1", language="en",
            )
            broadcast_dao.mark_failed_local(
                s, campaign=campaign, wa_digits=wa,
                error_code=fail_error_code, reason="test failure",
            )


class TestAutoPauseGuard:
    """Tests for the auto-pause quality guard added at the top of run()."""

    def _patch_get_session(self, monkeypatch, factory):
        monkeypatch.setattr(
            "whatsapp_delivery.tools.broadcast_send.get_session", factory
        )

    def _make_guard_args(
        self,
        *,
        campaign: str = "guard_campaign",
        min_sample: int = 10,
        max_fail_rate: float = 0.10,
        max_undeliverable: float = 0.40,
        tier: str = "T1",
    ) -> SimpleNamespace:
        return _make_args(
            campaign=campaign,
            tier=tier,
            min_sample=min_sample,
            max_fail_rate=max_fail_rate,
            max_undeliverable=max_undeliverable,
        )

    def test_guard_trips_on_high_fail_rate_returns_3(
        self, monkeypatch, sqlite_session_factory
    ):
        """Guard trips → run() returns 3 and performs ZERO sends or uploads."""
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_fail_rate"
        # 50 sent, 20 failed → fail_rate = 20/70 ≈ 0.286 > 0.10
        _seed_ledger(sqlite_session_factory, campaign=campaign, n_sent=50, n_failed=20)

        args = self._make_guard_args(campaign=campaign, min_sample=10)
        rows = [_row(f"9900{i}") for i in range(5)]

        send_mock = MagicMock(return_value="wamid.x")
        upload_mock = MagicMock(return_value="MEDIA_ID")

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            result = run(args, rows=rows)

        assert result == 3
        send_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_guard_excludes_marketing_cap_131049_from_fail_rate(
        self, monkeypatch, sqlite_session_factory
    ):
        """131049 (marketing-cap) is transient/retryable, excluded from the
        fail-rate — a campaign whose only failures are marketing-capped is NOT
        paused. Without the exclusion this would be 11/50 = 0.22 > 0.10 → trip.
        """
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_marketing_cap"
        # 39 sent + 11 failed(131049) → attempted=50; non-transient fails = 0
        _seed_ledger(
            sqlite_session_factory,
            campaign=campaign,
            n_sent=39,
            n_failed=11,
            fail_error_code=131049,
        )

        args = self._make_guard_args(campaign=campaign, min_sample=10, max_fail_rate=0.10)
        rows = [_row(f"9500{i}") for i in range(2)]

        send_mock = MagicMock(return_value="wamid.mc")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            result = run(args, rows=rows)

        assert result == 0
        assert send_mock.call_count == 2

    def test_guard_inactive_below_min_sample(
        self, monkeypatch, sqlite_session_factory
    ):
        """Below min_sample attempted rows the guard does NOT trip even with a high fail-rate."""
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_below_sample"
        # 5 sent, 5 failed → fail_rate = 0.5, but only 10 attempted < min_sample=50
        _seed_ledger(sqlite_session_factory, campaign=campaign, n_sent=5, n_failed=5)

        args = self._make_guard_args(campaign=campaign, min_sample=50, max_fail_rate=0.10)
        rows = [_row(f"9800{i}") for i in range(2)]

        send_mock = MagicMock(return_value="wamid.y")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            result = run(args, rows=rows)

        # Guard did not trip → normal send proceeds (returns 0)
        assert result == 0
        assert send_mock.call_count == 2

    def test_guard_passes_when_healthy(
        self, monkeypatch, sqlite_session_factory
    ):
        """Enough attempted rows but low fail / undeliverable rates → run proceeds normally."""
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_healthy"
        # 90 sent, 4 failed → fail_rate = 4/94 ≈ 0.043 < 0.10
        _seed_ledger(sqlite_session_factory, campaign=campaign, n_sent=90, n_failed=4)

        args = self._make_guard_args(campaign=campaign, min_sample=10, max_fail_rate=0.10)
        rows = [_row(f"9700{i}") for i in range(2)]

        send_mock = MagicMock(return_value="wamid.z")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            result = run(args, rows=rows)

        assert result == 0
        assert send_mock.call_count == 2

    def test_guard_trips_on_high_undeliverable_rate(
        self, monkeypatch, sqlite_session_factory
    ):
        """High undeliverable rate (error_code 131026) also trips the guard."""
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_undel"
        # 40 sent, 25 failed with error_code 131026 → undel_rate = 25/65 ≈ 0.385 < 0.40
        # Use 30 failed with 131026 → undel_rate = 30/70 ≈ 0.429 > 0.40
        _seed_ledger(
            sqlite_session_factory,
            campaign=campaign,
            n_sent=40,
            n_failed=30,
            fail_error_code=131026,
        )

        args = self._make_guard_args(campaign=campaign, min_sample=10, max_undeliverable=0.40)
        rows = [_row(f"9600{i}") for i in range(2)]

        send_mock = MagicMock(return_value="wamid.u")
        upload_mock = MagicMock(return_value="MEDIA_ID_U")

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            result = run(args, rows=rows)

        assert result == 3
        send_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_guard_trips_before_dry_run_returns_3_not_0(
        self, monkeypatch, sqlite_session_factory
    ):
        """A tripped guard must return 3 even in dry-run mode (no sends anyway, but exit code is 3)."""
        from whatsapp_delivery.tools.broadcast_send import run

        campaign = "guard_dryrun"
        # 50 sent, 20 failed → fail_rate > 0.10
        _seed_ledger(sqlite_session_factory, campaign=campaign, n_sent=50, n_failed=20)

        args = self._make_guard_args(campaign=campaign, min_sample=10)
        # Override to dry-run
        args.dry_run = True
        args.yes = False
        rows = [_row("99001")]

        send_mock = MagicMock(return_value="wamid.dr")
        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            result = run(args, rows=rows)

        assert result == 3
        send_mock.assert_not_called()


# ---------------------------------------------------------------------------
# I1 — --yes gate in run() (defense-in-depth)
# ---------------------------------------------------------------------------


class TestYesGate:
    """run() must refuse to send live without --yes, even if dry_run=False."""

    def _patch_get_session(self, monkeypatch, factory):
        monkeypatch.setattr(
            "whatsapp_delivery.tools.broadcast_send.get_session", factory
        )

    def test_run_live_without_yes_returns_2_and_no_sends(
        self, monkeypatch, sqlite_session_factory
    ):
        """run(dry_run=False, yes=False) must return 2 and perform zero sends/uploads."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001"), _row("91002")]
        # Explicitly: dry_run=False but yes=False — the forbidden live path.
        args = _make_args(dry_run=False, yes=False, video_media_id="MEDIA_ID")

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(return_value="wamid.should_not_happen")
        upload_mock = MagicMock(return_value="MEDIA_ID_SHOULD_NOT_HAPPEN")

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC, \
             patch("whatsapp_delivery.tools.broadcast_send.MetaClient") as MockMC:
            MockTC.return_value.send_template_with_components = send_mock
            MockMC.return_value.upload_media = upload_mock
            result = run(args, rows=rows)

        assert result == 2, f"Expected exit code 2, got {result}"
        send_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_run_live_with_yes_true_proceeds(
        self, monkeypatch, sqlite_session_factory
    ):
        """run(dry_run=False, yes=True) must pass the gate and proceed to send."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(dry_run=False, yes=True, video_media_id="MEDIA_ID")

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        send_mock = MagicMock(return_value="wamid.ok")

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = send_mock
            result = run(args, rows=rows)

        assert result == 0
        send_mock.assert_called_once()

    def test_run_dry_run_true_without_yes_still_ok(
        self, monkeypatch, sqlite_session_factory
    ):
        """Dry-run with yes=False must still return 0 (gate only blocks live path)."""
        from whatsapp_delivery.tools.broadcast_send import run

        rows = [_row("91001")]
        args = _make_args(dry_run=True, yes=False)

        self._patch_get_session(monkeypatch, sqlite_session_factory)
        monkeypatch.setattr("time.sleep", lambda _: None)

        with patch("whatsapp_delivery.tools.broadcast_send.TemplateClient") as MockTC:
            MockTC.return_value.send_template_with_components = MagicMock()
            result = run(args, rows=rows)

        assert result == 0


# ---------------------------------------------------------------------------
# I2 — WA_Digits normalization (digits-only)
# ---------------------------------------------------------------------------


class TestWaDigitsNormalization:
    """select_recipients must normalize WA_Digits to digits-only before matching."""

    def test_plus_prefix_stripped_and_excluded_when_suppressed(self):
        """'+91 99999-99999' must normalize to '919999999999' and match suppressed set."""
        rows = [
            {"WA_Digits": "+91 99999-99999", "Name (clean)": "Alice", "Tier label": "T1 Foo"},
            _row("91001"),
        ]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed={"919999999999"},
            already_done=set(),
            limit=100,
        )
        # '+91 99999-99999' normalizes to '919999999999' which is suppressed → excluded
        digits = [r.wa_digits for r in result]
        assert "919999999999" not in digits
        assert "91001" in digits

    def test_normalized_digits_stored_on_recipient(self):
        """Recipient.wa_digits must hold the normalized (digits-only) form."""
        rows = [
            {"WA_Digits": "+91 98765-43210", "Name (clean)": "Bob", "Tier label": "T1 Foo"},
        ]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed=set(),
            already_done=set(),
            limit=100,
        )
        assert len(result) == 1
        assert result[0].wa_digits == "919876543210"

    def test_already_done_normalized_comparison(self):
        """already_done set using normalized form must exclude the row."""
        rows = [
            {"WA_Digits": "+91 99999 99999", "Name (clean)": "Charlie", "Tier label": "T1 Foo"},
        ]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed=set(),
            already_done={"919999999999"},
            limit=100,
        )
        assert result == []

    def test_pure_digits_unchanged(self):
        """A plain digits-only WA_Digits must pass through unchanged."""
        rows = [_row("919876543210")]
        result = select_recipients(
            rows,
            tier="T1",
            suppressed=set(),
            already_done=set(),
            limit=100,
        )
        assert result[0].wa_digits == "919876543210"

    def test_spaces_and_hyphens_stripped(self):
        """Various formats with spaces/hyphens normalize to digits-only."""
        formats = [
            "+91-98765-43210",
            "91 98765 43210",
            "(91) 98765-43210",
        ]
        for fmt in formats:
            rows = [{"WA_Digits": fmt, "Name (clean)": "X", "Tier label": "T1 Foo"}]
            result = select_recipients(
                rows, tier="T1", suppressed=set(), already_done=set(), limit=100
            )
            assert len(result) == 1, f"Expected 1 result for format {fmt!r}"
            assert result[0].wa_digits == "919876543210", (
                f"Expected '919876543210', got {result[0].wa_digits!r} for {fmt!r}"
            )
