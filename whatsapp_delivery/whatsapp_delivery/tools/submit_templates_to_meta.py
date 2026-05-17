"""Submit pre-filed WhatsApp templates to Meta's Business Manager API.

This is a one-shot operator tool, invoked from the runbook
``casepilot/docs/runbooks/meta_template_submission.md``. It reads the
shared YAML registries at::

    whatsapp_delivery/templates/munshi/*.yml
    whatsapp_delivery/templates/nowlez/*.yml

and POSTs every template to Meta's Cloud API endpoint
``POST /v15.0/{WABA_ID}/message_templates``. Templates already present
on Meta's side -- in ANY status (PENDING, APPROVED, REJECTED) -- are
skipped so the script is idempotent and can be re-run safely after a
partial failure or after editing a single template's wording.

Each successful submission is also recorded in the ``audit_log`` table
with ``event_type='template.submitted'`` per Sub-project E plan §1.1.2,
so the operator has a tamper-evident paper trail of every Meta
filing.

Usage:

    # Dry-run (default for first invocation -- print what would POST
    # without touching Meta or the DB):
    python -m whatsapp_delivery.tools.submit_templates_to_meta --dry-run

    # Filter to a single brand or template prefix:
    python -m whatsapp_delivery.tools.submit_templates_to_meta --dry-run \
        --filter nowlez_

    # Real submission (requires the two env vars):
    META_ACCESS_TOKEN=EAAB... META_WABA_ID=1234567890 \
        python -m whatsapp_delivery.tools.submit_templates_to_meta

Exit codes:
    0  -- all templates processed (some may have been skipped).
    1  -- at least one POST failed (network error or 4xx). The script
          continues past per-template failures and reports the summary
          at the end; a non-zero exit code surfaces the failure to the
          shell so the operator knows to investigate.
    2  -- configuration error (missing env var in non-dry-run mode,
          unreadable YAML, etc.).
"""
from __future__ import annotations

import argparse
import json
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

# Repo-relative path to the per-brand YAML registries. Resolved once at
# import time so test harnesses can monkey-patch it via the env var
# ``WHATSAPP_TEMPLATES_DIR``.
_REGISTRY_DIR = Path(
    os.environ.get(
        "WHATSAPP_TEMPLATES_DIR",
        str(Path(__file__).resolve().parent.parent / "templates"),
    )
)


log = logging.getLogger("submit_templates_to_meta")


# ---------------------------------------------------------------------------
# Loading the per-brand YAML registries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateFiling:
    """One language-variant filing ready for Meta's API."""

    # Meta-side global name (e.g. "nowlez_new_order_v1" or "tomorrow_causelist_v1").
    full_name: str
    # Brand the template belongs to (munshi/nowlez); used for audit-log source.
    brand: str
    # Meta language code -- "en", "hi", "en_US", "hi_IN" depending on registry.
    language: str
    # The full request body the Cloud API expects, pre-built so dry-run
    # and live mode share identical payloads.
    body: dict[str, Any]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _components_for_language(lang_block: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a single language's YAML entry to Meta's components list.

    Meta expects an array of ``{type, ...}`` blocks. We emit at most one
    of each: ``HEADER`` (with optional text), ``BODY`` (always), and
    ``BUTTONS`` (when the language defines buttons). Variable
    declarations from the YAML are encoded as ``example`` payloads so
    Meta's reviewer sees a concrete sample of each placeholder.
    """
    components: list[dict[str, Any]] = []

    # Header. The YAML's `header` shape is either {type: text, text: ...}
    # or {type: document}; both map cleanly to Meta's expected payload.
    header = lang_block.get("header")
    if header is not None:
        block: dict[str, Any] = {"type": "HEADER"}
        h_type = (header.get("type") or "").lower()
        if h_type == "text":
            block["format"] = "TEXT"
            block["text"] = header.get("text", "")
        elif h_type == "document":
            block["format"] = "DOCUMENT"
        elif h_type == "image":
            block["format"] = "IMAGE"
        elif h_type == "video":
            block["format"] = "VIDEO"
        else:
            block["format"] = h_type.upper() or "TEXT"
        components.append(block)

    # Body (required by Meta).
    body_text = lang_block.get("body", "")
    body_block: dict[str, Any] = {"type": "BODY", "text": body_text}
    body_vars = [
        v for v in lang_block.get("variables", [])
        if (v.get("location") or "body") == "body"
    ]
    if body_vars:
        # Meta wants positional examples in the same order as {{1}}..{{N}};
        # we use the variable name itself as a placeholder so the reviewer
        # can see what each slot represents.
        body_vars_sorted = sorted(body_vars, key=lambda v: int(v["position"]))
        body_block["example"] = {
            "body_text": [[str(v["name"]) for v in body_vars_sorted]]
        }
    components.append(body_block)

    # Buttons (optional). Quick-reply and URL buttons each have their own
    # JSON shape on Meta's side -- we translate both.
    buttons_yaml = lang_block.get("buttons") or []
    if buttons_yaml:
        rendered_buttons: list[dict[str, Any]] = []
        for b in buttons_yaml:
            b_type = (b.get("type") or "").lower()
            if b_type == "quick_reply":
                rendered_buttons.append({
                    "type": "QUICK_REPLY",
                    "text": b.get("text", ""),
                })
            elif b_type == "url":
                entry: dict[str, Any] = {
                    "type": "URL",
                    "text": b.get("text", ""),
                    "url": b.get("url", ""),
                }
                # If the URL contains a {{N}} suffix, Meta requires an
                # example URL. We synthesize one using the var name.
                url_vars = [
                    v for v in lang_block.get("variables", [])
                    if (v.get("location") or "") == "button_url"
                ]
                if url_vars:
                    sample = b.get("url", "").replace("{{1}}", "example-id")
                    entry["example"] = [sample]
                rendered_buttons.append(entry)
            elif b_type == "phone_number":
                rendered_buttons.append({
                    "type": "PHONE_NUMBER",
                    "text": b.get("text", ""),
                    "phone_number": b.get("phone_number", ""),
                })
        if rendered_buttons:
            components.append({"type": "BUTTONS", "buttons": rendered_buttons})

    return components


def _yaml_to_filings(yaml_doc: dict[str, Any], *, brand: str) -> list[TemplateFiling]:
    """Convert one YAML doc (e.g. munshi/cause_list.yml) to a flat list."""
    filings: list[TemplateFiling] = []
    for tpl in yaml_doc.get("templates", []):
        full_name = tpl.get("full_name") or tpl["name"]
        category = (tpl.get("category") or "UTILITY").upper()
        for lang_code, lang_block in (tpl.get("languages") or {}).items():
            body: dict[str, Any] = {
                "name": full_name,
                "category": category,
                "language": lang_code,
                "components": _components_for_language(lang_block),
            }
            filings.append(TemplateFiling(
                full_name=full_name,
                brand=brand,
                language=lang_code,
                body=body,
            ))
    return filings


def load_all_filings(registry_dir: Path = _REGISTRY_DIR) -> list[TemplateFiling]:
    """Walk the brand subfolders and aggregate every language filing.

    Returns one ``TemplateFiling`` per (template, language) tuple.
    """
    out: list[TemplateFiling] = []
    if not registry_dir.exists():
        raise FileNotFoundError(f"Template registry not found at: {registry_dir}")

    for brand_dir in sorted(registry_dir.iterdir()):
        if not brand_dir.is_dir():
            continue
        brand = brand_dir.name
        for yml in sorted(brand_dir.glob("*.yml")):
            try:
                doc = _load_yaml(yml)
            except yaml.YAMLError as e:
                raise RuntimeError(f"Invalid YAML at {yml}: {e}") from e
            out.extend(_yaml_to_filings(doc, brand=brand))
    return out


# ---------------------------------------------------------------------------
# Meta API interaction
# ---------------------------------------------------------------------------


def fetch_existing_template_names(
    *, waba_id: str, access_token: str,
) -> set[tuple[str, str]]:
    """Return the set of ``(name, language)`` tuples already on Meta.

    We paginate through Meta's ``message_templates`` list endpoint and
    record every name+language combo regardless of status (PENDING,
    APPROVED, REJECTED). The caller uses this set to skip
    already-submitted templates -- per the runbook, fixing a REJECTED
    template means editing the YAML and using the Meta dashboard to
    delete the old filing, not resubmitting from this script.
    """
    seen: set[tuple[str, str]] = set()
    url = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    params: dict[str, Any] = {"limit": 1000}

    while url:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params if "?" not in url else None,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        for entry in data.get("data", []):
            name = entry.get("name")
            lang = entry.get("language")
            if name and lang:
                seen.add((name, lang))
        # Cursor pagination: Meta returns `paging.next` as a full URL
        # when more pages exist.
        url = (data.get("paging") or {}).get("next")
        params = {}
    return seen


def submit_filing(
    filing: TemplateFiling,
    *,
    waba_id: str,
    access_token: str,
) -> dict[str, Any]:
    """POST one template-language filing to Meta. Returns the parsed JSON."""
    url = f"{_GRAPH_BASE}/{waba_id}/message_templates"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=filing.body,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Audit log integration (best-effort, optional)
# ---------------------------------------------------------------------------


def _record_audit(filing: TemplateFiling, meta_response: dict[str, Any]) -> None:
    """Insert a ``template.submitted`` audit row, best-effort.

    Failure here is logged but does NOT fail the submission -- the
    Meta-side filing is the source of truth and the operator can
    reconcile manually. The optional import keeps the script runnable
    in environments without the data_access package on PYTHONPATH.
    """
    try:
        from data_access.engine import get_session
        from data_access.models.audit import AuditLog
    except ImportError:
        log.debug("data_access not importable; skipping audit_log insert")
        return

    # Audit source is constrained to {'munshi','nowlez','identity','system'}
    # by audit_source_check. Brand maps directly.
    source = filing.brand if filing.brand in ("munshi", "nowlez") else "system"
    try:
        with get_session() as session:
            session.add(AuditLog(
                event_type="template.submitted",
                source=source,
                metadata_={
                    "template_name": filing.full_name,
                    "language": filing.language,
                    "category": filing.body.get("category"),
                    "meta_template_id": meta_response.get("id"),
                    "meta_status": meta_response.get("status"),
                },
            ))
    except Exception as e:  # pragma: no cover -- best-effort
        log.warning(
            "audit_log insert failed for %s/%s: %s",
            filing.full_name, filing.language, e,
        )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _matches_filter(filing: TemplateFiling, prefix: str | None) -> bool:
    return prefix is None or filing.full_name.startswith(prefix)


def run(
    *,
    dry_run: bool,
    filter_prefix: str | None,
    registry_dir: Path = _REGISTRY_DIR,
) -> int:
    """Drive the submission. Returns an exit code suitable for ``sys.exit``."""
    log.info("Loading template registry from %s", registry_dir)
    try:
        all_filings = load_all_filings(registry_dir)
    except (FileNotFoundError, RuntimeError) as e:
        log.error("Failed to load registry: %s", e)
        return 2

    selected: list[TemplateFiling] = [
        f for f in all_filings if _matches_filter(f, filter_prefix)
    ]
    log.info(
        "Loaded %d total filings (%d distinct templates); %d match filter %r",
        len(all_filings),
        len({f.full_name for f in all_filings}),
        len(selected),
        filter_prefix,
    )

    if dry_run:
        log.info("DRY-RUN: skipping Meta API calls and audit_log inserts")
        for f in selected:
            log.info(
                "DRY-RUN would POST: brand=%s name=%s language=%s body=%s",
                f.brand, f.full_name, f.language,
                json.dumps(f.body, ensure_ascii=False)[:200],
            )
        log.info("DRY-RUN complete -- %d filings would be submitted", len(selected))
        return 0

    # Live mode: env vars are mandatory.
    waba_id = os.environ.get("META_WABA_ID")
    access_token = os.environ.get("META_ACCESS_TOKEN")
    if not waba_id or not access_token:
        log.error(
            "META_WABA_ID and META_ACCESS_TOKEN must be set (got waba=%s token_set=%s)",
            bool(waba_id), bool(access_token),
        )
        return 2

    log.info("Fetching existing templates from Meta for idempotency check...")
    try:
        existing = fetch_existing_template_names(
            waba_id=waba_id, access_token=access_token,
        )
    except httpx.HTTPError as e:
        log.error("Could not list existing templates: %s", e)
        return 1
    log.info("Meta currently has %d filings on this WABA", len(existing))

    submitted = 0
    skipped = 0
    failed = 0
    for f in selected:
        key = (f.full_name, f.language)
        if key in existing:
            log.info(
                "SKIP (already filed): %s [%s]", f.full_name, f.language,
            )
            skipped += 1
            continue
        try:
            response = submit_filing(
                f, waba_id=waba_id, access_token=access_token,
            )
        except httpx.HTTPError as e:
            failed += 1
            log.error(
                "FAILED %s [%s]: %s",
                f.full_name, f.language, e,
            )
            continue
        log.info(
            "SUBMITTED %s [%s] -> id=%s status=%s",
            f.full_name, f.language,
            response.get("id"), response.get("status"),
        )
        _record_audit(f, response)
        submitted += 1

    log.info(
        "Summary: submitted=%d skipped=%d failed=%d total=%d",
        submitted, skipped, failed, len(selected),
    )
    return 0 if failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="submit_templates_to_meta",
        description="Submit YAML-registered WhatsApp templates to Meta.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be requests; skip Meta API calls and audit inserts.",
    )
    p.add_argument(
        "--filter",
        dest="filter_prefix",
        default=None,
        help="Only process templates whose full_name starts with this prefix "
             "(e.g. --filter nowlez_).",
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    return run(dry_run=args.dry_run, filter_prefix=args.filter_prefix)


if __name__ == "__main__":
    sys.exit(main())
