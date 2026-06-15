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
        import pandas as pd

        from whatsapp_delivery.tools.broadcast_send import main

        # Write a minimal xlsx so _load_rows can read it
        xlsx_path = tmp_path / "broadcast.xlsx"
        df = pd.DataFrame(
            [
                {
                    "WA_Digits": "91001",
                    "Name (clean)": "Alice",
                    "Tier label": "T1 Premium",
                },
                {
                    "WA_Digits": "91002",
                    "Name (clean)": "Bob",
                    "Tier label": "T2 Standard",
                },
            ]
        )
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Broadcast List", index=False)

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
