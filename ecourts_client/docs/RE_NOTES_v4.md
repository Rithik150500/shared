# eCourts mobile API v4.0 — reverse-engineering notes (2026-07-02)

eCourts retired the **v3.0** mobile endpoints and cut the server over to **v4.0**
on **2026-07-02** (broke between 09:25 and 17:41 UTC). Old paths now 404; the 404
HTML page fails `_crypto.decrypt_response` (`bytes.fromhex("<!DOCTYPE…")`) — that
`ValueError: non-hexadecimal number … at position 0` was the platform-wide
symptom (NOT captcha, NOT rate-limiting).

Source of truth: **eCourts Services v4.0.1 APK** (`in.gov.ecourts.eCourtsServices`,
React-Native / Hermes bytecode v96 bundle `assets/index.android.bundle`,
disassembled with `hermes-dec` `hbc-disassembler`) + live verification against
`app.ecourts.gov.in`.

## Phase 1 — auth/transport (DONE, in `_session.py`, unit-validated 62/62)

| Aspect | v3.0 | v4.0 |
|---|---|---|
| Base URL | `/ecourt_mobile_{DC,HC}/` | `/services_{DC,HC}_4.0/` |
| Bootstrap endpoint | `appReleaseWebService.php` | same (still returns `.token`) |
| Bootstrap field | `version` | **`appVersion`** (value `4.0.1`) |
| `uid` | `<uuid>:<pkg>` | **`<pkg>`** i.e. `in.gov.ecourts.eCourtsServices` |
| Bearer | `Bearer <encrypt_request(jwt)>` | **`Bearer <raw jwt>`** |
| Common envelope | version_number/uid/lang | interceptor injects **`uid`** into every params |
| AES keys (req `4D62…397A`, resp `3273…4B62`) | — | **UNCHANGED** |
| Error msg key | `msg` | **`Msg`** (401 = `UnAuthorized`/`Not in session`) |

The token is a real HS256 JWT (`iss` = the scope base URL, has `iat`/`nbf`/`exp`).
Wrong `uid` (v3 `<uuid>:<pkg>` form) → `version_compatible:"S1", token:null`.
Verified live: bootstrap→token and `stateWebService.php`→`{"states":[…]}` both work.

## Phase 2 — response/endpoint restructuring

v4.0 also **restructured responses and renamed/added endpoints**. The auth fix
alone does NOT restore case fetch.

### DONE — HC fetch-by-CNR (`caseHistoryWebService.php` + `parsers/case_history.py`)
Payload is still `{cino}` (+ injected uid) and returns `{"history": {...}}`, but
the v3 HTML tables became **structured JSON** (rewrite validated live +
62/62 tests). Concrete v4 shapes:
- `act`: `list[{actCodeName, actSectionName}]` (was HTML)
- `interimOrder` / `finalOrder`: `list[{order_id, order_date1|order_date1f,
  order_details, filename, caseno, cCode, appFlag, state_cd, dist_cd,
  court_code}]` (was HTML). `filename` is root-relative (`/orders/YYYY/…_N.pdf`).
- `historyOfCaseHearing`: null on disposed cases (structured list when present —
  field names still to confirm on a pending case).
- Flat metadata present: `pet_name/res_name/pet_adv/res_adv`, `date_of_filing`,
  `date_next_list`, `desgname`, `court_name/state_name/district_name`,
  `type_name/fil_type_name` (used as stage fallback — `purpose_name` often null),
  `case_no`, `reg_no/reg_year`, `under_act1..4`.
`parse_case_history` now type-dispatches (v4 list vs v3 HTML) so it stays
back-compatible with the fixtures.

### DONE — order-PDF fetch (POST), search renames, DC fetch
- **Order PDF (POST)** — v4 is TWO steps: **POST** `display_pdf_new.php?params=<enc>`
  (encrypted order fields `{filename,caseno,cCode,appFlag,state_cd,dist_cd,court_code,
  bilingual_flag:"1"}`, raw-jwt bearer) → returns `{"pdf_url":"https://csc.ecourts.gov.in/
  helpdesk_alias/<hash>.pdf"}`, then **GET** that signed alias URL for the bytes.
  Body-POST returns `{status:N}`; params MUST be on the query string. Implemented in
  `pdf.py` (`encode_v4_order` → `displaypdf:` scheme; `fetch_order_pdf(session,url)`
  does POST→pdf_url→GET); `_session._send` gained a `method` arg; the clients'
  `fetch_pdf` route through it. Validated live: real `%PDF-1.4` bytes.
- **Search endpoints renamed** — `caseNumberWebService`→`caseTypesWebService.php`
  (`list_case_types`, HC + DC) and `showDataWebService`→`searchByPartyName.php`
  (party search, HC + DC); `caseNumberSearch.php` unchanged. The existing
  `parse_case_types` (#-delimited `code~name`) and `parse_case_number_search`
  (`{0:{caseNos:[{cino}]}}`) already matched v4 → no parser change.
- **DC fetch** — `caseHistoryWebService.php` keys on `cino` (v3 `cinum` →
  `error_ERROR_State_code1`); `listOfCasesWebService.php` works with `{cino}` + uid.

### REMAINING
- **DC case-type lister court_code** — `caseTypesWebService.php` for District rejects
  every court_code tried (court-no `1/2/3`, est `1270001`, envelope) with
  `error_ERROR_courtcode4`. v4 changed the DC case-type param; needs disasm RE.
  Blocks DC search-by-case-number (party-search avoids it). DC fetch-by-CNR is fine.
- **Hearing history v4 shape** — `historyOfCaseHearing` is null on disposed cases;
  confirm the structured-list field names on a pending case.

Full v4 `.php` inventory + disasm on the droplet: `~/ecourts_re/disasm.hasm`,
saved HC sample `~/ecourts_re/hc_case_history.json`, APK `~/ecourts_re/ecourts.apk`.

## Repro / tooling
- APK: `apkpure` direct — `https://d.apkpure.net/b/APK/in.gov.ecourts.eCourtsServices?version=latest` (30 MB, v4.0.1).
- Disassemble: `pip install hermes-dec` → `hbc-disassembler assets/index.android.bundle disasm.hasm`.
- Strings appear as `# String: '...'`; endpoints via `grep "String: '[a-z]*\.php'"`.
- Live tests must come from an **India egress** (the prod droplet) and be
  **rate-limited** — eCourts throttles bursts to 405/HTML for ~15–30 min.

## Burst throttle (405/HTML) — signature + mitigation (added 2026-07-03)

On 2026-07-03 ~09:50 IST a bulk case-add (Gujarat HC book; one
`display_pdf_new.php` POST per order → a burst of hundreds of app-host calls in
minutes) tripped eCourts' IP throttle. Signature, captured live:

    HTTP 405, Content-Type text/html, no Server header, body:
    "<!DOCTYPE html>… <center><strong>Welcome User Search Page not Found
    here</strong></center> …"  (~228 bytes)

This is **transient** (the IP recovers on its own in ~15–30 min — verified: a
200 + valid `token` returned ~20 min later, endpoints unchanged) and is
distinct from a genuine API rotation (which 404s the path permanently). Both,
unfortunately, share the same downstream symptom if unhandled: the HTML body
reaches `_crypto.decrypt_response`, whose `bytes.fromhex("<!DOCTYPE…")` raises
`ValueError: non-hexadecimal number found in fromhex() arg at position 0`.

Mitigations in `_session.py`:
- **Classify, don't crash.** `_send` maps HTTP 405 → `RateLimited` (not retried
  — hammering resets the window; the shared `ecourts_global` circuit breaker
  still trips after the failure threshold). Any other non-envelope body →
  `CourtSiteDown` with a diagnostic snippet, guarded by `_RESPONSE_ENVELOPE_RE`
  before `decrypt_response`. The API layer turns both into a clean 502
  "temporarily unavailable" instead of an opaque 500.
- **Don't trip it in the first place** (opt-in). A process-wide `_RateGate` can
  pace outbound app-host calls to `ECOURTS_MIN_REQUEST_INTERVAL_SECONDS`.
  **Default 0 (off)** — the reactive path above (405→RateLimited + circuit
  breaker) is the day-1 posture; set e.g. `0.34` (≈3 req/s) to enable proactive
  prevention if bursts recur. Worst-case added interactive latency is then
  bounded by `max_concurrency × interval` (the semaphore caps concurrency); bulk
  background operations slow proportionally, which is the intended trade.
