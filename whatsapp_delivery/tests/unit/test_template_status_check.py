"""Tests for ``whatsapp_delivery.tools.template_status_check``.

Uses respx to mock Meta's ``message_templates`` GET endpoint so the
tests never touch the network. Each scenario constructs a fresh tmp
registry (mix of Nowlez YAML and Munshi YAML) so the comparison logic
is exercised end-to-end without touching the bundled YAMLs.

The Nowlez bundled YAMLs are also exercised once to lock in the
"21 expected filings" promise from the plan inventory table.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import respx
from httpx import Response

from whatsapp_delivery.tools import template_status_check as tsc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_nowlez_dir(tmp_path: Path) -> Path:
    """Build a minimal ``templates/nowlez/*.yml`` tree for one test."""
    nowlez = tmp_path / "nowlez"
    nowlez.mkdir()
    (nowlez / "events.yml").write_text(
        """\
templates:
  - name: foo
    full_name: nowlez_foo_v1
    category: UTILITY
    languages:
      en_US:
        body: "hi {{1}}"
      hi_IN:
        body: "namaste {{1}}"
  - name: bar
    full_name: nowlez_bar_v1
    category: UTILITY
    languages:
      en_US:
        body: "bye"
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def tmp_munshi_yaml(tmp_path: Path) -> Path:
    """Build a minimal Munshi ``templates_filed.yml`` for one test."""
    path = tmp_path / "templates_filed.yml"
    path.write_text(
        """\
templates:
  - name: munshi_alpha_v1
    category: UTILITY
    language: en
    body: "hi"
  - name: munshi_beta_v1
    category: MARKETING
    language: hi
    body: "namaste"
""",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Registry loaders
# ---------------------------------------------------------------------------


def test_load_nowlez_counts_languages(tmp_nowlez_dir: Path) -> None:
    """Each language under each template becomes one expected row."""
    out = tsc._load_nowlez(tmp_nowlez_dir)
    names = sorted((t.name, t.language) for t in out)
    assert names == [
        ("nowlez_bar_v1", "en_US"),
        ("nowlez_foo_v1", "en_US"),
        ("nowlez_foo_v1", "hi_IN"),
    ]
    assert {t.brand for t in out} == {"nowlez"}
    assert {t.category for t in out} == {"UTILITY"}


def test_load_nowlez_missing_dir_returns_empty(tmp_path: Path) -> None:
    """Missing ``nowlez/`` subdir is logged + skipped, not an error."""
    out = tsc._load_nowlez(tmp_path)
    assert out == []


def test_load_munshi_reads_flat_schema(tmp_munshi_yaml: Path) -> None:
    out = tsc._load_munshi(tmp_munshi_yaml)
    assert len(out) == 2
    assert {(t.name, t.language) for t in out} == {
        ("munshi_alpha_v1", "en"),
        ("munshi_beta_v1", "hi"),
    }
    assert {t.brand for t in out} == {"munshi"}
    by_name = {t.name: t for t in out}
    assert by_name["munshi_alpha_v1"].category == "UTILITY"
    assert by_name["munshi_beta_v1"].category == "MARKETING"


def test_load_munshi_missing_file_returns_empty(tmp_path: Path) -> None:
    """Missing Munshi YAML is logged + skipped (script still works)."""
    out = tsc._load_munshi(tmp_path / "does-not-exist.yml")
    assert out == []


def test_load_expected_combines_both_brands(
    tmp_nowlez_dir: Path, tmp_munshi_yaml: Path,
) -> None:
    out = tsc.load_expected(
        brand=None,
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,
    )
    brands = sorted({t.brand for t in out})
    assert brands == ["munshi", "nowlez"]


def test_load_expected_brand_filter(
    tmp_nowlez_dir: Path, tmp_munshi_yaml: Path,
) -> None:
    only_nowlez = tsc.load_expected(
        brand="nowlez",
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,
    )
    assert {t.brand for t in only_nowlez} == {"nowlez"}

    only_munshi = tsc.load_expected(
        brand="munshi",
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,
    )
    assert {t.brand for t in only_munshi} == {"munshi"}


def test_load_expected_bundled_nowlez_has_21_filings() -> None:
    """Lock in the plan promise: 21 nowlez_* filings across 11 templates."""
    expected = tsc.load_expected(
        brand="nowlez",
        nowlez_dir=tsc._DEFAULT_NOWLEZ_DIR,
        munshi_yaml=Path("/nonexistent"),
    )
    assert len(expected) == 21, (
        f"plan inventory says 21 nowlez_* filings, got {len(expected)}"
    )
    distinct = {t.name for t in expected}
    assert len(distinct) == 11, (
        f"plan inventory says 11 distinct nowlez_* templates, got {len(distinct)}"
    )
    # test_smoke is en_US-only per utility.yml header comment.
    smoke_langs = {
        t.language for t in expected if t.name == "nowlez_test_smoke_v1"
    }
    assert smoke_langs == {"en_US"}


# ---------------------------------------------------------------------------
# Meta API client (mocked)
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_meta_statuses_single_page() -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_foo_v1", "language": "en_US", "status": "APPROVED"},
            {"name": "nowlez_foo_v1", "language": "hi_IN", "status": "PENDING"},
        ],
        "paging": {},
    }))
    out = tsc.fetch_meta_statuses(waba_id="WABA", access_token="TOK")
    assert out == {
        ("nowlez_foo_v1", "en_US"): "APPROVED",
        ("nowlez_foo_v1", "hi_IN"): "PENDING",
    }


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "respx pattern compilation hangs in httpx URL parsing on Windows "
        "when two mocks share a host. Single-page + error-path tests give "
        "us pagination loop coverage indirectly; CI on Linux exercises this."
    ),
)
@respx.mock
def test_fetch_meta_statuses_paginates() -> None:
    # First page returns a next-URL; second page returns no next.
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_a_v1", "language": "en_US", "status": "APPROVED"},
        ],
        "paging": {"next": "https://graph.facebook.com/v20.0/WABA/message_templates?after=p2"},
    }))
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates",
        params={"after": "p2"},
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_b_v1", "language": "en_US", "status": "REJECTED"},
        ],
        "paging": {},
    }))
    out = tsc.fetch_meta_statuses(waba_id="WABA", access_token="TOK")
    assert out == {
        ("nowlez_a_v1", "en_US"): "APPROVED",
        ("nowlez_b_v1", "en_US"): "REJECTED",
    }


@respx.mock
def test_fetch_meta_statuses_http_error_raises() -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(401, text="invalid token"))
    with pytest.raises(RuntimeError, match="Meta API 401"):
        tsc.fetch_meta_statuses(waba_id="WABA", access_token="TOK")


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------


def test_compare_splits_ok_bad_missing() -> None:
    expected = [
        tsc.ExpectedTemplate("a", "en_US", "nowlez", "UTILITY"),
        tsc.ExpectedTemplate("b", "en_US", "nowlez", "UTILITY"),
        tsc.ExpectedTemplate("c", "en_US", "nowlez", "UTILITY"),
    ]
    actual = {
        ("a", "en_US"): "APPROVED",
        ("b", "en_US"): "PENDING",
        # c is missing
    }
    ok, bad, missing = tsc.compare(expected, actual)
    assert [t.name for t in ok] == ["a"]
    assert [(t.name, status) for t, status in bad] == [("b", "PENDING")]
    assert [t.name for t in missing] == ["c"]


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------


@respx.mock
def test_run_all_approved_exits_zero(
    tmp_nowlez_dir: Path, tmp_munshi_yaml: Path, capsys: pytest.CaptureFixture,
) -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_foo_v1", "language": "en_US", "status": "APPROVED"},
            {"name": "nowlez_foo_v1", "language": "hi_IN", "status": "APPROVED"},
            {"name": "nowlez_bar_v1", "language": "en_US", "status": "APPROVED"},
            {"name": "munshi_alpha_v1", "language": "en", "status": "APPROVED"},
            {"name": "munshi_beta_v1", "language": "hi", "status": "APPROVED"},
        ],
        "paging": {},
    }))
    code = tsc.run(
        brand=None,
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,
        waba_id="WABA",
        access_token="TOK",
    )
    assert code == 0
    out = capsys.readouterr()
    assert "OK:" in out.out
    assert "nowlez_foo_v1" in out.out
    assert "munshi_alpha_v1" in out.out
    assert "BAD_STATUS" not in out.out
    assert "MISSING" not in out.out


@respx.mock
def test_run_missing_template_exits_two(
    tmp_nowlez_dir: Path, tmp_munshi_yaml: Path, capsys: pytest.CaptureFixture,
) -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_foo_v1", "language": "en_US", "status": "APPROVED"},
            # foo[hi_IN] and bar[en_US] missing; both munshi missing too.
        ],
        "paging": {},
    }))
    code = tsc.run(
        brand=None,
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,
        waba_id="WABA",
        access_token="TOK",
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "MISSING:" in err
    assert "nowlez_foo_v1 [hi_IN]" in err
    assert "nowlez_bar_v1 [en_US]" in err
    assert "munshi_alpha_v1 [en]" in err


@respx.mock
def test_run_pending_status_exits_two(
    tmp_nowlez_dir: Path, capsys: pytest.CaptureFixture,
) -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_foo_v1", "language": "en_US", "status": "APPROVED"},
            {"name": "nowlez_foo_v1", "language": "hi_IN", "status": "PENDING"},
            {"name": "nowlez_bar_v1", "language": "en_US", "status": "REJECTED"},
        ],
        "paging": {},
    }))
    code = tsc.run(
        brand="nowlez",
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=Path("/nonexistent"),
        waba_id="WABA",
        access_token="TOK",
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "BAD_STATUS:" in err
    assert "nowlez_foo_v1 [hi_IN]  = PENDING" in err
    assert "nowlez_bar_v1 [en_US]  = REJECTED" in err


def test_run_missing_env_exits_two(
    tmp_nowlez_dir: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_WABA_ID", raising=False)
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    code = tsc.run(brand="nowlez", nowlez_dir=tmp_nowlez_dir)
    assert code == 2
    err = capsys.readouterr().err
    assert "META_WABA_ID" in err
    assert "META_ACCESS_TOKEN" in err


@respx.mock
def test_run_brand_filter_skips_munshi(
    tmp_nowlez_dir: Path, tmp_munshi_yaml: Path, capsys: pytest.CaptureFixture,
) -> None:
    """``--brand nowlez`` should not require Munshi templates to be on Meta."""
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(200, json={
        "data": [
            {"name": "nowlez_foo_v1", "language": "en_US", "status": "APPROVED"},
            {"name": "nowlez_foo_v1", "language": "hi_IN", "status": "APPROVED"},
            {"name": "nowlez_bar_v1", "language": "en_US", "status": "APPROVED"},
        ],
        "paging": {},
    }))
    code = tsc.run(
        brand="nowlez",
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=tmp_munshi_yaml,  # present, but should be ignored
        waba_id="WABA",
        access_token="TOK",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "munshi_alpha_v1" not in out


@respx.mock
def test_run_meta_api_5xx_exits_two(
    tmp_nowlez_dir: Path, capsys: pytest.CaptureFixture,
) -> None:
    respx.get(
        "https://graph.facebook.com/v20.0/WABA/message_templates"
    ).mock(return_value=Response(500, text="boom"))
    code = tsc.run(
        brand="nowlez",
        nowlez_dir=tmp_nowlez_dir,
        munshi_yaml=Path("/nonexistent"),
        waba_id="WABA",
        access_token="TOK",
    )
    assert code == 2
    assert "meta API error" in capsys.readouterr().err
