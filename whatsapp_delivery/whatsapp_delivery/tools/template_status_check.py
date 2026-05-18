"""Verify every WhatsApp template in the YAML registries is APPROVED at Meta.

Mirrors Munshi's ``deploy/scripts/template_status_check.py`` but reads
**both** the Munshi-side ``deploy/templates_filed.yml`` (if present)
AND the Nowlez-side ``whatsapp_delivery/templates/nowlez/*.yml``
registries. Calls Meta's
``GET /v15.0/{WABA_ID}/message_templates`` endpoint, paginates through
every filing on the WABA, and reports per template whether Meta's
status matches the expected ``APPROVED``.

Designed to run on demand (after a filing pass) and as a periodic CI
job — Meta may auto-pause a template if quality drops, so a green
check today does not imply green check tomorrow.

Usage::

    META_WABA_ID=4469866243246469 META_ACCESS_TOKEN=EAAB... \\
        python -m whatsapp_delivery.tools.template_status_check

    # Filter to one brand:
    python -m whatsapp_delivery.tools.template_status_check --brand nowlez
    python -m whatsapp_delivery.tools.template_status_check --brand munshi

The Munshi YAML location can be overridden via ``MUNSHI_TEMPLATES_YAML``
(default: ``C:/Project3/0705/deploy/templates_filed.yml``). The Nowlez
registry root can be overridden via ``WHATSAPP_TEMPLATES_DIR``
(default: ``whatsapp_delivery/templates``).

Exit codes:

* ``0`` -- every expected template is registered and APPROVED.
* ``2`` -- at least one expected template is MISSING / PENDING /
  REJECTED / PAUSED, **or** Meta API / config errored. Stderr lists
  every failing entry plus a summary count.

Munshi's original script uses exit ``1`` for status failures and ``2``
for network/config errors. This unified version collapses both into
``2`` per the task brief ("Exits 0 if all approved, 2 if any
missing/rejected/pending"). When the script is invoked from CI that
distinguishes between the two cases, parse stderr for the ``BAD_STATUS:``
/ ``MISSING:`` lines instead of the exit code.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml


_GRAPH_VERSION = "v15.0"
_GRAPH_BASE = f"https://graph.facebook.com/{_GRAPH_VERSION}"
_TIMEOUT = 30

# Locations of the two registry sources. Both are overridable via env
# vars so test harnesses and alternate deployments can redirect them.
_DEFAULT_NOWLEZ_DIR = Path(__file__).resolve().parent.parent / "templates"
_DEFAULT_MUNSHI_YAML = Path("C:/Project3/0705/deploy/templates_filed.yml")

_NOWLEZ_DIR = Path(os.environ.get("WHATSAPP_TEMPLATES_DIR", str(_DEFAULT_NOWLEZ_DIR)))
_MUNSHI_YAML = Path(os.environ.get("MUNSHI_TEMPLATES_YAML", str(_DEFAULT_MUNSHI_YAML)))


log = logging.getLogger("template_status_check")


# ---------------------------------------------------------------------------
# Expected-template representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedTemplate:
    """One (name, language) tuple we expect to find APPROVED at Meta."""

    name: str
    language: str
    brand: str
    category: str  # UTILITY / MARKETING / AUTHENTICATION


# ---------------------------------------------------------------------------
# Registry loaders
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_nowlez(nowlez_dir: Path) -> list[ExpectedTemplate]:
    """Walk ``templates/nowlez/*.yml`` and emit one row per (template, lang).

    The Nowlez YAML schema is::

        templates:
          - name: <short>
            full_name: <meta_name>
            category: UTILITY
            languages:
              en_US: { ... }
              hi_IN: { ... }

    where ``full_name`` is the name actually filed at Meta (the short
    ``name`` is the registry-internal identifier).
    """
    out: list[ExpectedTemplate] = []
    nowlez_subdir = nowlez_dir / "nowlez"
    if not nowlez_subdir.exists():
        log.warning("Nowlez registry not found at %s; skipping", nowlez_subdir)
        return out

    for yml in sorted(nowlez_subdir.glob("*.yml")):
        try:
            doc = _load_yaml(yml)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Invalid YAML at {yml}: {e}") from e
        for tpl in doc.get("templates") or []:
            full_name = tpl.get("full_name") or tpl.get("name")
            category = (tpl.get("category") or "UTILITY").upper()
            for lang_code in (tpl.get("languages") or {}).keys():
                out.append(ExpectedTemplate(
                    name=full_name,
                    language=lang_code,
                    brand="nowlez",
                    category=category,
                ))
    return out


def _load_munshi(munshi_yaml: Path) -> list[ExpectedTemplate]:
    """Read Munshi's ``templates_filed.yml`` (flat list of filings).

    The Munshi YAML schema is::

        templates:
          - name: <meta_name>
            category: UTILITY|MARKETING
            language: en|hi
            body: "..."
            variables: [...]

    Returns an empty list (with a warning) if the file is missing — this
    lets the script work in environments where the Munshi monorepo isn't
    cloned alongside the shared package.
    """
    if not munshi_yaml.exists():
        log.warning("Munshi registry not found at %s; skipping", munshi_yaml)
        return []

    try:
        doc = _load_yaml(munshi_yaml)
    except yaml.YAMLError as e:
        raise RuntimeError(f"Invalid YAML at {munshi_yaml}: {e}") from e

    out: list[ExpectedTemplate] = []
    for tpl in doc.get("templates") or []:
        name = tpl.get("name")
        if not name:
            continue
        category = (tpl.get("category") or "UTILITY").upper()
        language = tpl.get("language") or "en"
        out.append(ExpectedTemplate(
            name=name,
            language=language,
            brand="munshi",
            category=category,
        ))
    return out


def load_expected(
    *,
    brand: str | None = None,
    nowlez_dir: Path = _NOWLEZ_DIR,
    munshi_yaml: Path = _MUNSHI_YAML,
) -> list[ExpectedTemplate]:
    """Return the union of expected filings across both registries.

    ``brand`` filters to a single brand ('nowlez' or 'munshi'); ``None``
    returns both.
    """
    expected: list[ExpectedTemplate] = []
    if brand in (None, "nowlez"):
        expected.extend(_load_nowlez(nowlez_dir))
    if brand in (None, "munshi"):
        expected.extend(_load_munshi(munshi_yaml))
    return expected


# ---------------------------------------------------------------------------
# Meta API
# ---------------------------------------------------------------------------


def fetch_meta_statuses(
    *,
    waba_id: str,
    access_token: str,
) -> dict[tuple[str, str], str]:
    """Page through Meta's templates list; return ``{(name, lang): status}``.

    Uses ``fields=name,language,status,category&limit=100`` per the
    task brief. Pagination follows the cursor in ``paging.next`` — Meta
    bakes the query params into the next URL so we drop ``params`` on
    follow-up requests.
    """
    out: dict[tuple[str, str], str] = {}
    url: str | None = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    params: dict[str, Any] | None = {
        "fields": "name,language,status,category",
        "limit": 100,
    }
    # Use a managed Client so respx can patch the transport cleanly in
    # tests, and so SSL/connection state is reused across pagination hops.
    # The module-level ``httpx.get`` creates a fresh Client per call which
    # bypasses respx's transport mocking on sync paths.
    with httpx.Client(timeout=_TIMEOUT) as client:
        while url:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Meta API {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            for entry in data.get("data") or []:
                name = entry.get("name")
                lang = entry.get("language")
                status = entry.get("status")
                if name and lang:
                    out[(name, lang)] = status or "UNKNOWN"
            url = (data.get("paging") or {}).get("next")
            params = None  # subsequent URLs embed params already
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def compare(
    expected: Iterable[ExpectedTemplate],
    actual: dict[tuple[str, str], str],
) -> tuple[list[ExpectedTemplate], list[tuple[ExpectedTemplate, str]], list[ExpectedTemplate]]:
    """Split expected templates into (ok, bad_status, missing).

    * ``ok`` -- present at Meta with ``status == APPROVED``.
    * ``bad_status`` -- present but not APPROVED (PENDING / REJECTED / ...).
    * ``missing`` -- not present at all.
    """
    ok: list[ExpectedTemplate] = []
    bad: list[tuple[ExpectedTemplate, str]] = []
    missing: list[ExpectedTemplate] = []
    for tpl in expected:
        key = (tpl.name, tpl.language)
        if key not in actual:
            missing.append(tpl)
            continue
        if actual[key] != "APPROVED":
            bad.append((tpl, actual[key]))
            continue
        ok.append(tpl)
    return ok, bad, missing


def run(
    *,
    brand: str | None,
    nowlez_dir: Path = _NOWLEZ_DIR,
    munshi_yaml: Path = _MUNSHI_YAML,
    waba_id: str | None = None,
    access_token: str | None = None,
) -> int:
    """Drive the check. Returns an exit code suitable for ``sys.exit``."""
    waba_id = waba_id or os.environ.get("META_WABA_ID")
    access_token = access_token or os.environ.get("META_ACCESS_TOKEN")
    if not waba_id or not access_token:
        print(
            "META_WABA_ID and META_ACCESS_TOKEN must be set",
            file=sys.stderr,
        )
        return 2

    try:
        expected = load_expected(
            brand=brand,
            nowlez_dir=nowlez_dir,
            munshi_yaml=munshi_yaml,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"registry load error: {e}", file=sys.stderr)
        return 2

    if not expected:
        print(
            f"no expected templates loaded for brand={brand!r}",
            file=sys.stderr,
        )
        return 2

    try:
        actual = fetch_meta_statuses(
            waba_id=waba_id, access_token=access_token,
        )
    except (httpx.HTTPError, RuntimeError) as e:
        print(f"meta API error: {e}", file=sys.stderr)
        return 2

    ok, bad, missing = compare(expected, actual)

    # Stable, alphabetical reporting per category.
    for tpl in sorted(ok, key=lambda t: (t.brand, t.name, t.language)):
        print(f"OK:         {tpl.name} [{tpl.language}]  APPROVED  ({tpl.brand})")
    for tpl, status in sorted(bad, key=lambda x: (x[0].brand, x[0].name, x[0].language)):
        print(
            f"BAD_STATUS: {tpl.name} [{tpl.language}]  = {status}  ({tpl.brand})",
            file=sys.stderr,
        )
    for tpl in sorted(missing, key=lambda t: (t.brand, t.name, t.language)):
        print(
            f"MISSING:    {tpl.name} [{tpl.language}]  (filed in YAML but not registered)  ({tpl.brand})",
            file=sys.stderr,
        )

    summary = (
        f"Summary: expected={len(expected)} ok={len(ok)} "
        f"bad={len(bad)} missing={len(missing)}"
    )
    if bad or missing:
        print(summary, file=sys.stderr)
        return 2
    print(summary)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="template_status_check",
        description=(
            "Verify every template in the YAML registries is APPROVED at Meta."
        ),
    )
    p.add_argument(
        "--brand",
        choices=["nowlez", "munshi"],
        default=None,
        help="Restrict the check to one brand's registry (default: both).",
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    return run(brand=args.brand)


if __name__ == "__main__":
    sys.exit(main())
