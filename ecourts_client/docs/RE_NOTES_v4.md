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

### REMAINING
- **Order PDF fetch (POST)**: v4 fetches order PDFs via **POST** `display_pdf_new.php`
  with params `{filename, caseno, cCode, appFlag, state_cd, dist_cd, court_code,
  bilingual_flag:"1"}` (the order dict carries all of them). The naive static URL
  `https://app.ecourts.gov.in{filename}` **405s**. `pdf.py` needs a v4 POST path;
  until then order PDFs 404-degrade (order stored, case still lands). The parser
  currently emits the static URL as `order_url` (absolute, so no MissingSchema).
- **Search endpoints renamed** (needed to resolve case-number → CNR from
  screenshots): v3 `caseNumberWebService.php` (case-type codes, `list_case_types`)
  → **`caseTypesWebService.php`**; v3 `showDataWebService.php` (party search) →
  **`searchByPartyName.php`**; `caseNumberSearch.php` still present (verify
  payload/response). Response shapes likely changed → re-check the `parsers/`.
- **District fetch flow**: DC `caseHistoryWebService.php` errors
  `error_ERROR_State_code1` with `{cino}` alone — the DC multi-step flow
  (`listOfCasesWebService.php` → `caseHistoryWebService.php {cinum,…}`) needs the
  establishment envelope (`state_code`/`dist_code`/`court_code`).

Full v4 `.php` inventory + disasm on the droplet: `~/ecourts_re/disasm.hasm`,
saved HC sample `~/ecourts_re/hc_case_history.json`, APK `~/ecourts_re/ecourts.apk`.

## Repro / tooling
- APK: `apkpure` direct — `https://d.apkpure.net/b/APK/in.gov.ecourts.eCourtsServices?version=latest` (30 MB, v4.0.1).
- Disassemble: `pip install hermes-dec` → `hbc-disassembler assets/index.android.bundle disasm.hasm`.
- Strings appear as `# String: '...'`; endpoints via `grep "String: '[a-z]*\.php'"`.
- Live tests must come from an **India egress** (the prod droplet) and be
  **rate-limited** — eCourts throttles bursts to 405/HTML for ~15–30 min.
