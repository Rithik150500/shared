"""YAML-backed template registry.

Loads every ``*.yml`` under this package on first access and exposes
``get_template(full_name, language)`` for the dispatch worker. The result is
a :class:`TemplateAccessor` that knows the language-specific body, variable
ordering, and button shape — used by ``dispatch.worker._do_send_template``
to translate a ``dict`` of variable names into the positional list Meta
expects.

The loader caches the parsed result in a module-level dict so the YAML
files are parsed exactly once per process. Tests can force a reparse by
calling :func:`invalidate_registry_cache`.

**D-8 (audit fix):** Pre-fix the loader was wrapped in
``functools.lru_cache(maxsize=1)``. That had two failure modes that
weren't worth the perf win on a sub-10ms parse:

1. ``lru_cache`` poisoned the cache on first-call failure. If
   ``_load_registry()`` raised (bad YAML, file race), every subsequent
   call re-raised the *cached* exception without retrying — the only
   recovery was process restart.
2. There was no public invalidation knob, so a YAML edit was stale
   until the next deploy.

The replacement uses a plain module-level dict + a sentinel flag, so a
failed parse leaves the cache untouched (next call retries the file
read) and operators / tests have an explicit
``invalidate_registry_cache()`` to force a reparse.

YAML schema (per plan §7.2.1):

.. code-block:: yaml

    templates:
      - name: new_order_v1
        full_name: nowlez_new_order_v1
        category: UTILITY
        languages:
          en_US:
            header: { type: document }            # OR { type: text, text: "..." }
            body: |
              <Meta-style body with {{1}}, {{2}}, ... placeholders>
            buttons:
              - { type: url, text: "Open", url: "https://app.nowlez.in/cases/{{4}}" }
            variables:
              - { name: case_title, position: 1, location: body }
              - { name: case_id,    position: 4, location: button_url }
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from whatsapp_delivery.errors import TemplateNotFound


VarLocation = Literal["body", "button_url", "header"]


@dataclass(frozen=True)
class TemplateVariable:
    """One ``{{N}}`` slot in a body or button URL.

    ``location`` distinguishes body vars (the WhatsApp body component) from
    URL-button vars (the URL suffix Meta interpolates into the button) —
    both are positional but live in different ``components[]`` entries.
    """

    name: str
    position: int
    location: VarLocation


@dataclass(frozen=True)
class TemplateButton:
    """A single template button.

    ``type='url'`` carries a ``url`` field; ``type='quick_reply'`` does not.
    """

    type: str  # "url" | "quick_reply"
    text: str
    url: str | None = None


@dataclass(frozen=True)
class TemplateLanguage:
    """Per-language template content."""

    body: str
    header_type: str  # "text" | "document"
    header_text: str | None
    buttons: tuple[TemplateButton, ...]
    variables: tuple[TemplateVariable, ...]


@dataclass(frozen=True)
class Template:
    """A registered template across all its filed languages.

    ``name`` is the short name (``new_order_v1``); ``full_name`` is the
    brand-prefixed Meta-side name (``nowlez_new_order_v1``). All callers
    look templates up by ``full_name`` because that's the name they'll see
    on Meta side.
    """

    name: str
    full_name: str
    category: str
    languages: dict[str, TemplateLanguage]

    def has_document_header(self) -> bool:
        return any(l.header_type == "document" for l in self.languages.values())

    def body_variables_in_order(self) -> list[TemplateVariable]:
        any_lang = next(iter(self.languages.values()))
        return sorted(
            [v for v in any_lang.variables if v.location == "body"],
            key=lambda v: v.position,
        )

    def button_url_variables_in_order(self) -> list[TemplateVariable]:
        any_lang = next(iter(self.languages.values()))
        return sorted(
            [v for v in any_lang.variables if v.location == "button_url"],
            key=lambda v: v.position,
        )


@dataclass(frozen=True)
class TemplateAccessor:
    """Per-language view returned by :func:`get_template`.

    The dispatch worker calls this; it knows both the language-agnostic
    variable ordering (used to translate dict → positional list) and the
    language-specific body/buttons (for assertions in tests).
    """

    template: Template
    language_spec: TemplateLanguage
    language: str

    @property
    def full_name(self) -> str:
        return self.template.full_name

    @property
    def category(self) -> str:
        return self.template.category

    @property
    def body(self) -> str:
        return self.language_spec.body

    @property
    def buttons(self) -> tuple[TemplateButton, ...]:
        return self.language_spec.buttons

    def has_document_header(self) -> bool:
        return self.language_spec.header_type == "document"

    def body_variables_in_order(self) -> list[TemplateVariable]:
        return sorted(
            [v for v in self.language_spec.variables if v.location == "body"],
            key=lambda v: v.position,
        )

    def button_url_variables_in_order(self) -> list[TemplateVariable]:
        return sorted(
            [v for v in self.language_spec.variables if v.location == "button_url"],
            key=lambda v: v.position,
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_PKG_ROOT = Path(__file__).resolve().parent


def _parse_entry(entry: dict) -> Template:
    langs: dict[str, TemplateLanguage] = {}
    for code, lang in (entry.get("languages") or {}).items():
        header = lang.get("header") or {}
        langs[code] = TemplateLanguage(
            body=lang["body"],
            header_type=header.get("type", "text"),
            header_text=header.get("text"),
            buttons=tuple(
                TemplateButton(**b) for b in (lang.get("buttons") or [])
            ),
            variables=tuple(
                TemplateVariable(**v) for v in (lang.get("variables") or [])
            ),
        )
    if not langs:
        raise ValueError(
            f"template {entry.get('full_name')!r} has no languages defined"
        )
    return Template(
        name=entry["name"],
        full_name=entry["full_name"],
        category=entry["category"],
        languages=langs,
    )


# Module-level cache. ``None`` means "not yet loaded (or invalidated)";
# any dict (including the empty one) means "successfully loaded — reuse".
# The sentinel pattern is what gives us exception-safety: a failed parse
# inside ``_load_registry`` leaves ``_REGISTRY`` at None, so the NEXT
# call retries the file read instead of re-raising a cached exception
# the way ``lru_cache`` did.
_REGISTRY: dict[str, Template] | None = None


def _load_registry() -> dict[str, Template]:
    """Parse every ``templates/**/*.yml`` once per process.

    Walks the bundled tree (``templates/munshi/*.yml`` and
    ``templates/nowlez/*.yml``) and keys the result by ``full_name``.
    Collisions raise ``ValueError`` so two files can't accidentally redefine
    the same Meta-side template.

    Caches the result in the module-level ``_REGISTRY`` dict; repeated
    calls return the same object identity. On parse failure ``_REGISTRY``
    stays at None so the next call retries (this is the audit-fix
    difference vs the old ``lru_cache`` which cached the exception).
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    out: dict[str, Template] = {}
    for yml in sorted(_PKG_ROOT.rglob("*.yml")):
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        for entry in data.get("templates") or []:
            tmpl = _parse_entry(entry)
            if tmpl.full_name in out:
                raise ValueError(
                    f"duplicate template full_name {tmpl.full_name!r}: "
                    f"defined twice (second occurrence in {yml})"
                )
            out[tmpl.full_name] = tmpl
    # Assign at the end so a mid-loop ValueError leaves the cache at None
    # and operators can re-run after fixing the YAML.
    _REGISTRY = out
    return _REGISTRY


def invalidate_registry_cache() -> None:
    """Drop the cached registry so the next call reparses the YAML files.

    Useful in two cases:

    * Tests that mutate the bundled YAML files mid-suite (rare).
    * Operators that edit a template file and want the new content
      without a process restart.

    Note: callers already holding a ``TemplateAccessor`` continue to see
    the old data — only future ``get_template`` / ``list_templates``
    calls see the reload.
    """
    global _REGISTRY
    _REGISTRY = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_template(full_name: str, language: str) -> TemplateAccessor:
    """Return the per-language accessor for ``full_name``.

    Raises :class:`TemplateNotFound` if either the template name or the
    language variant is unknown.
    """
    reg = _load_registry()
    tmpl = reg.get(full_name)
    if tmpl is None:
        raise TemplateNotFound(full_name)
    lang_spec = tmpl.languages.get(language)
    if lang_spec is None:
        raise TemplateNotFound(f"{full_name} ({language})")
    return TemplateAccessor(template=tmpl, language_spec=lang_spec, language=language)


def list_templates() -> list[Template]:
    """All registered :class:`Template` objects (one per ``full_name``)."""
    return list(_load_registry().values())


__all__ = [
    "Template",
    "TemplateAccessor",
    "TemplateButton",
    "TemplateLanguage",
    "TemplateVariable",
    "VarLocation",
    "get_template",
    "invalidate_registry_cache",
    "list_templates",
]
