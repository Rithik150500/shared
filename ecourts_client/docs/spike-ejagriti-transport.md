# e-Jagriti (Consumer forum) transport spike — Phase-2 gate

**Date:** 2026-07-04 · **Verdict: GO** (was GO-WITH-FALLBACK; the one open item —
the exact working POST body — was live-confirmed from our own stack, see §6).

Consumer forum = **NCDRC / SCDRC / DCDRC**, served by the unified **e-Jagriti**
portal (e-jagriti.gov.in, which merged eDaakhil/Confonet/OCMS/CMS on 2025-01-01).
This is the Phase-2 reference forum for the multi-forum expansion. Build the
Consumer adapter on e-Jagriti's **public plain-JSON REST API** — do **not**
screen-scrape the `/advance-case-search` SPA (its image captcha gates only the
browser UI, not the JSON path).

## 1. Base + transport
- **Base URL:** `https://e-jagriti.gov.in/services`
- Plain JSON over HTTPS (nginx → Spring Boot). Response envelope:
  `{ "data": [...], "message": str, "error": "false"|"true" (STRING), "status": int }`.
- **Auth-free & cookie-free** for the tracking surface (bare request → 200). No
  JWT/API-key/login/CSRF for the four endpoints in §2. *Selectively* gated
  siblings (do NOT use): `/master/master/v2/caseCategory`, every
  `getCaseHistory*` / `getCaseDetailsByCaseNumber` variant, and the
  `/case/caseFilingService/v2/*` **filing/write** side all return
  `401 "Access Denied !! Full authentication is required"`.
- **Captcha is field-scoped, not global.** The basic case-number+commission
  search DTO has **no** captcha field (confirmed by bundle decompile + live
  200). Only the *advance/free-text* search DTO carries a required captcha. So
  our primary path (search by case number) is **captcha-free**.
- Send a browser-like `User-Agent` + `Origin: https://e-jagriti.gov.in` +
  `Referer` defensively (NOT enforced — bare curl worked — but cheap insurance).

## 2. The 3-step commission-scoped flow
There is **no global CNR-style key**; lookup is scoped to a *commission* + a
*date window*. Resolve the commission, then search.

1. **`GET /report/report/getStateCommissionAndCircuitBench`** — enumerate all
   State Commissions + circuit benches. No params.
   → `data:[{ commissionId:int (8-digit), commissionNameEn, circuitAdditionBenchStatus:bool, activeStatus:bool }]`
   (e.g. `11290000`=KARNATAKA, `11280000`=ANDHRA PRADESH, `11350000`=ANDAMAN NICOBAR; NCDRC = a top-level id).
2. **`GET /report/report/getDistrictCommissionByCommissionId?commissionId={stateId}`**
   — drill to the leaf district commission.
   → `data:[{ commissionId:int, commissionNameEn, ... }]`
   (e.g. `11290525`=Bangalore Urban, `11350602`=Andaman).
3. **`POST /case/caseFilingService/v2/getCaseDetailsBySearchType`**
   (`Content-Type: application/json`) — the core status+history endpoint. It is
   a **search within one commission over a date window**, not a `getByCNR`.
   Request body:
   ```json
   {
     "commissionId": <leaf id>,
     "dateRequestType": 1,          // 1 = by filing date, 2 = by disposal date
     "fromDate": "YYYY-MM-DD",
     "toDate":   "YYYY-MM-DD",
     "judgeId":  "",                // "" unless serchType=7
     "page": 0,                      // 0-based
     "size": 30,
     "serchType": 1,                 // 1=caseNo, 2=complainant, 3=respondent,
                                     // 4=complAdv, 5=respAdv, 6=industry, 7=judge
     "serchTypeValue": "<case number substring>"
   }
   ```
   **⚠️ The API's own misspellings `serchType` / `serchTypeValue` MUST be sent
   verbatim.** Response rows carry: `caseNumber` (e.g. `"SC/29/A/1006/2024"`),
   `complainantName`, `respondentName`, `complainantAdvocateName`,
   `respondentAdvocateName`, `caseFilingDate`, `caseStageName` (e.g.
   `"DISPOSED OFF"`), `dateOfHearing`, `orderDate`, `dateOfDisposal`,
   `filingReferenceNumber:int`, `orderAvailabilityStatusId`, and order/judgment
   PDFs inline as base64 (`documentBase64` / `judgmentOrderDocumentBase64`,
   decode → `%PDF-1.7`) or as `orderDocumentPath` / `judgemtmentDocumentPath[sic]`.

*(Supporting: `POST /master/master/v2/getJudgeListForHearing?commissionId=..&activeStatus=true`
is public and only needed to resolve `judgeId` for `serchType=7` — not the
primary path.)*

## 3. Response → generic `Case` mapping
`forum='consumer'`, `source='ejagriti_auto'`, **`cnr` stays NULL** (nullable-cnr
multiforum migration already supports this), `forum_case_ref <- caseNumber`.
- `parties[]` ← `complainantName` + `respondentName` (+ `additionalComplainantList` / `additionalRespondantList[sic]`)
- `filing_date` ← `caseFilingDate`
- `next_hearing_date` ← `dateOfHearing`
- `hearing_history[]` ← `caseStageName` progression (this endpoint IS the history; the dedicated `getCaseHistory*` endpoints are 401-gated — do not use)
- `interim_orders[]` / `final_orders[]` ← `orderDate` + PDF (prefer inline base64; else the `*DocumentPath`); mirror PDFs into the **same order-PDF ingest path as the eCourts adapter**
- `case_status` ← `caseStageName` (+ `dateOfDisposal` when disposed)
- Persist extra: `complainant_advocate`, `respondent_advocate`, `order_date`, `disposal_date`, `filingReferenceNumber`, and **both** the state + leaf `commissionId` (so refresh re-queries without re-resolving names).
- **Parsing quirks:** `error` is the STRING `"false"`/`"true"`; misspelled keys `additionalRespondant*`, `judgemtmentDocument*`.

## 4. Identifiers / tiers
Commission hierarchy mirrors NCDRC: **State Commission / circuit bench** (8-digit
`commissionId`) → **District Commission** (leaf `commissionId`). NCDRC = a
top-level id. Human case number e.g. `SC/29/A/1006/2024`. Internal numeric id =
`filingReferenceNumber`. `cnrNumber` sometimes appears in the response schema
(possible future CNR lookup) but no public fetch-by-CNR endpoint is confirmed.
No paid tiers — it's the same free public API the website consumes.

## 5. User inputs the flow must collect
1. Commission **level + name** (NCDRC / a State Commission / a District
   Commission) → resolve to a leaf `commissionId` via the two lister GETs
   (present as cascading dropdowns; **cache** the lists — they change rarely).
2. **Case number** (→ `serchTypeValue`, `serchType=1`).
3. A **date window** + `dateRequestType` (1=filing/2=disposal) — REQUIRED (it's a
   search). UX can default a wide window from the case-number's year suffix so
   the user usually need only pick commission + case number. On refresh, reuse
   the stored leaf `commissionId` + a rolling window.

## 6. Live confirmation (2026-07-04, from our stack)
All three steps returned **HTTP 200** via `curl` with no auth/cookie:
- `getStateCommissionAndCircuitBench` → 200 (ANDAMAN NICOBAR = 11350000)
- `getDistrictCommissionByCommissionId?commissionId=11350000` → 200 (Andaman = 11350602)
- `getCaseDetailsBySearchType` with the body above (commissionId 11350602, serchType 1) →
  `200 {"message":"Case Detail successfully fetched.","data":[],"error":"false"}`
  — **body shape accepted** (empty `data` only because that small commission had
  no matching filings in the narrow test window). A populated row with real
  parties/dates/base64-PDF was separately observed live during the spike.

## 7. Contingency & guardrails
Same risk profile as the eCourts v3→v4 break: undocumented, unofficial NIC API
that can change/lock without notice. Build defensively:
- Schema-tolerant parsing (honor the string `error`, the misspelled keys).
- Cache master data (state/district lists); serialize per-case with small
  concurrency; exponential backoff on 5xx/timeout; realistic UA + Origin/Referer.
- **Alert on a sustained 401/500 shift** — the canary for an NIC version bump.
- Map failures into the shared `CourtSiteDown`/`RateLimited`/`BlockedByGeoIP`
  taxonomy; use a **per-forum circuit breaker** (`forum_consumer`) so a Consumer
  outage can't trip the eCourts breaker.
- **Legal:** per-user/per-case fetches only, no bulk harvesting; conservative
  rate limits; treat captcha as a signal (our path is captcha-free — keep it
  that way, don't touch the advance-search captcha). Have the engagement owner
  confirm risk appetite before Phase 2 ships (DPDP §3(c)(ii) public-data
  exemption vs IT Act §43 — unresolved, per the plan).

Fallbacks if the v2 path shifts: (a) the older `GET /report/report/getCauseTitleListByCompany`
name-search; (b) last resort — HTML+OCR of `/advance-case-search` (Playwright +
ddddocr) — heavy, captcha-solving, avoid unless the API fully locks.

## 8. Next steps (Phase 2 build)
1. Scaffold `shared/ecourts_client/consumer/` mirroring the eCourts layout:
   `_session.py` (plain-JSON session, no AES/JWT), `client.py` (`ConsumerClient`
   with `capabilities` + `fetch_case`/`search_by_*` matching the DC/HC method
   names + the adapter registry), `parsers.py` (→ generic `Case`), `routing.py`
   (commission resolve + ref validation).
2. Capture **one populated** live row (a real Consumer case number + its
   commission) as a parser fixture before finalizing the field mapping.
3. Register the adapter; per-forum circuit breaker; flip `/api/config` Consumer
   `capability` → `search`; scheduler forum-branch (`scheduler.py:539`) before
   any auto-refresh of NULL-cnr Consumer cases.

## Sources
Live probes 2026-07-04 (getStateCommissionAndCircuitBench, getDistrictCommissionByCommissionId,
getCaseDetailsBySearchType — all 200; caseCategory / getCommissions / getCaseHistory* — 401);
reference wrappers `tejodeepmitraroy/lexi-e-jagriti-takehome-project` (primary — pins the base URL
+ the 3 paths + serchType 1-7 / dateRequestType 1-2) and `sayaksamanta10176/jagriti-case-engine`
(corroborating + the getCauseTitleListByCompany fallback); negative refs
`Ajitsingh4362/ejagriti-scraper` and `Vinay1611/E-Jagriti-Data-Pipeline` (HTML+captcha path — avoid);
CRA bundle decompile `/static/js/main.97b58a6b.js` (DTOs, captcha scoping, field names).
