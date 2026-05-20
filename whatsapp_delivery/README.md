# whatsapp_delivery

Shared Meta WhatsApp Cloud API delivery client for Nowlez and Munshi.

## Install (development)

```
pip install -e ../../shared/whatsapp_delivery[test]
```

## Public API

See `whatsapp_delivery/__init__.py` for re-exports. Anything imported via submodules is internal.

## Env vars

See `whatsapp_delivery/config.py` (`WhatsAppConfig`).

## Operator tools

Two CLIs under `whatsapp_delivery/tools/`:

### `submit_templates_to_meta`

One-shot tool that POSTs every YAML-registered template to Meta. See the
runbook at `casepilot/docs/runbooks/meta_template_submission.md`.

```
python -m whatsapp_delivery.tools.submit_templates_to_meta --dry-run
python -m whatsapp_delivery.tools.submit_templates_to_meta --filter nowlez_
```

### `template_status_check`

Verifies every expected template (from both the Nowlez registry under
`templates/nowlez/*.yml` and Munshi's `0705/deploy/templates_filed.yml`,
if reachable) is APPROVED at Meta. Exit code 0 = all approved, 2 = at
least one missing / pending / rejected, or config / API error.

```
META_WABA_ID=4469866243246469 META_ACCESS_TOKEN=EAAB... \
    python -m whatsapp_delivery.tools.template_status_check

# Filter to a single brand:
python -m whatsapp_delivery.tools.template_status_check --brand nowlez
python -m whatsapp_delivery.tools.template_status_check --brand munshi
```

Default registry locations (overridable via env):

- `WHATSAPP_TEMPLATES_DIR` -- root of the Nowlez registry
  (default: `whatsapp_delivery/templates`)
- `MUNSHI_TEMPLATES_YAML` -- Munshi's flat YAML
  (default: `C:/Project3/0705/deploy/templates_filed.yml`)

The Nowlez filing-sheet runbook is at
`casepilot/docs/runbooks/nowlez_template_filing_sheet.md`.
