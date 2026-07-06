# Plan — Tribunal order documents (NCLAT PDFs + TDSAT order-text)

## Context

The Wave-0 tribunal adapters (NCLAT/TDSAT/DRT/DRAT) are live but surface **no
order documents** (`orders=[]`, except NCLAT which populates OrderRefs with a
**broken** direct-`.pdf` URL that 404s). This adds order documents where the
portals actually expose them, verified live (2026-07-06):

- **NCLAT** publishes real order **PDFs**, but via a `download` endpoint (a form
  POST), not the direct `.pdf` path the current adapter builds (that 404s).
- **TDSAT** orders are **HTML pages** (`daily_order_view.php` → `orderp.php`) — no
  PDF; the order **text** is in the page.
- **DRT/DRAT** expose **no** order surface on the public CIS — nothing to do.

**The casepilot pipeline is already forum-agnostic** (mapped this session): a
fetched `Case.orders[OrderRef]` with `inline_pdf_b64` is decoded → stored → served
→ previewed via `_ingest_inline_order_pdfs` (the Consumer seam), and `order_text`
renders as a timeline entry. The **one gap**: the forum path does NOT auto-download
orders that only carry an `order_url` (a `# deferred` comment confirms it). So the
clean approach is **adapter-inlined** documents — no new download-wiring in
casepilot.

## Decisions (owner-approved)
- Scope: **NCLAT order PDFs + TDSAT order text**. DRT: nothing (no surface).
- NCLAT fetch: **inline the latest N order PDFs** per fetch, `N` configurable
  (`TRIBUNAL_MAX_INLINE_ORDERS`, default **3**; raise / set 0 = all). Bounded by
  default (add-case stays fast, avoids the burst that tripped eCourts throttling);
  operator can opt into full history. Typical NCLAT cases have ≤3 orders, so the
  default already captures ~all for most cases.

## Implementation

### shared `ecourts_client` (the bulk of the work)
- **`tribunal/kinds/nclat.py`** — replace the broken direct-`.pdf` OrderRef URL with
  the real download flow:
  - **CONFIRMED endpoint (live browser capture 2026-07-07):** `POST /display-board/view_order`
    with **`multipart/form-data`** body `filing_no` + `_token` (the same session CSRF
    scraped for `view_details`) + `order_date` (`YYYY-MM-DD`, from `order_history[].order_date`) +
    `bench_name` (location, e.g. `delhi`). Response = the order **PDF** bytes. Returned
    a real PDF for Company Appeal(AT)(Ins) 1/2024's Final Order. ⚠ MUST be multipart —
    a urlencoded body hangs the server (in `requests`: `files={k:(None,v) …}`). Not
    `download` (commented-out .ajax path) and not the direct `.pdf` URL (404).
  - In `SupremeCourtClient`-style session: after `view_details`, for the newest
    `max_inline_orders` rows of `order_history`, POST `download`, validate the
    response starts with `%PDF`, and set `OrderRef(order_date=…, order_id=…,
    inline_pdf_b64=<b64>)`. Orders beyond the cap get an OrderRef with metadata
    only (date/id, no bytes) so the timeline still lists them.
  - Add `max_inline_orders` to the client (read from env `TRIBUNAL_MAX_INLINE_ORDERS`,
    default 3) — a session/client attr, not a fetch_case param (keeps the
    `fetch_case(identifier)` Protocol signature intact).
- **`tribunal/kinds/tdsat.py`** — populate `orders` with `order_text`:
  - Parse the order-sheet links (`popsurety_pet_adv_name('<b64>')` → `daily_order_view.php?filing_no=<b64>`) and pair each with its hearing date from the proceeding table (best-effort; where pairing is ambiguous, attach the order to its nearest proceeding row).
  - For the newest `max_inline_orders`, GET `daily_order_view.php`→`orderp.php`, strip to text, set `OrderRef(order_date, order_id, order_text=<text>)`. No PDF (none exists) → timeline entry only.
- **`tribunal/_session.py`** (or per-client) — a small `download_pdf`/`get_text` helper reusing the existing session (cookie + `_token`); map failures into the shared taxonomy (never fail the whole `fetch_case` on an order-fetch error — orders are best-effort, like the eCourts `_download_new_order_pdfs` skip-on-error).
- Tests: NCLAT order → `inline_pdf_b64` set + `%PDF` validated (mocked download); TDSAT order → `order_text` set; cap honored (only newest N inlined); order-fetch error doesn't sink the case.

### casepilot `backend` (minimal — verify + one likely hook)
- **Verify** `create_tribunal_case` calls `_ingest_inline_order_pdfs` (as
  `create_consumer_case` does at ~L2049). If it doesn't, add that one call
  (mirrors consumer) so add-time NCLAT PDFs get stored; `refresh_forum_case`
  already calls it (~L1937), so refresh is covered.
- `order_text` needs **no** backend change — `_shared_case_to_forum_flat` already
  maps it to the order `description` in `case_detail_json` → timeline.
- No change to storage/serve/preview/timeline (forum-agnostic).

### bot (optional, secondary)
- The `[See orders]` button flow already renders `Case.orders`; confirm it shows
  the tribunal orders (metadata + count). No new work expected.

## Verification (end-to-end)
- **Local:** shared unit tests (NCLAT inline-PDF + cap + error-skip; TDSAT order_text); `ruff`.
- **Live (prod egress):** fetch NCLAT `delhi:33:1:2023` → assert an order's `inline_pdf_b64` decodes to bytes starting `%PDF`; fetch a TDSAT case → assert `order_text` non-empty. Confirm the download endpoint's exact field names with one live POST first (the `fn_downloaddetails` payload is RE'd, not yet live-confirmed).
- **Deployed:** after pin-bump + deploy (bot-first then casepilot, the standard order), add an NCLAT case on web → order PDF previews in the timeline; refresh backfills. Spot-check in the casepilot container.

## Risks
- **Throttle** — bounded by the latest-N cap + the existing `MIN_REQUEST_INTERVAL`/per-forum breaker. Full-history (`N=0/all`) reintroduces burst risk; document it on the knob. Most cases have ≤3 orders → default is effectively complete.
- **NCLAT download endpoint** — the `POST download` payload (`_token`/`order_date`/`bench_name`/id) is RE'd from `case_status.js`; **live-confirm the exact field names + that the response is the PDF** before building the parser (one probe). Fallback: if `download` needs a field we can't supply, orders degrade to metadata-only (timeline entries, no document) — no regression.
- **TDSAT order↔date pairing** — 18 order-sheet links vs 15 hearings (separate section); pairing is heuristic. Acceptable: worst case an order attaches to an approximate date; the order text is still surfaced.
- **`fetch_case` latency** — inlining N PDFs adds N sequential fetches; keep N small (default 3) and best-effort (skip-on-error) so a slow/withheld order never blocks the case add.
- Deploy needs a shared pin bump on both backends (bot-first) — same cadence as the Wave-0 follow-ups.

## Critical files
- `shared/ecourts_client/ecourts_client/tribunal/kinds/{nclat,tdsat}.py` + `tribunal/_session.py` (+ tests).
- `casepilot/backend/preprocessing.py` — verify/wire `_ingest_inline_order_pdfs` in `create_tribunal_case` (one call if missing).
