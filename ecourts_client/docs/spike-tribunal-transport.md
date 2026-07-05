# Tribunal transport spike — Wave-0 (TDSAT / DRT-DRAT / CESTAT / NCLAT)

**Date:** 2026-07-06 · **Method:** search-form/endpoint pages fetched LIVE from the
production DigitalOcean egress (`ssh case-tracker` → `curl`, browser UA), then each
form's transport contract extracted and **adversarially verified against the raw
HTML** (8-agent workflow: extract → verify; verdicts below). This closes the
"exact POST params / endpoints / CSRF / option codes" thin-evidence items for the
four no-new-infra Wave-0 tribunals so [[tribunal-expansion-plan]] T3 Wave-0
adapters can be coded. Every target returned **HTTP 200** from our prod IP.

> Scope note: this maps the **request** contract (verified from static HTML). The
> **response** HTML→`Case` mapping for each still needs ONE live query with a real
> case number — listed per-target under "Open (needs live query)". No speculative
> enumeration was done; capture responses at a real user-add moment / with a known
> public case, per the DPDP/§43 discipline in [[tribunal-expansion-plan]].

---

## TDSAT — verdict: partial (core Case-No path fully literal)
- **Base:** `https://tdsat.gov.in/Delhi/services/casestatus.php` (single New Delhi bench)
- **Transport:** `POST` → `checkhomedetail1.php` (relative to `.../Delhi/services/`), `application/x-www-form-urlencoded`
- **CSRF:** none · **Captcha:** none ✓
- **Fields (Case-No mode):** `pet_type` (mode: `1`=Case No. Wise, `2`=Diary No. Wise), `casetype` (numeric code), `caseno`, `caseyear`, `submit1=Search` *(the submit field is literally `submit1`, not `submit`)*
- **`casetype` codes (11, NON-contiguous — code 6 absent):** 1=Broadcasting Petition, 2=Telecom Petition, 3=Broadcasting Appeal, 4=Telecom Appeal, 5=Misc Application, 7=Review Application, 8=E A, 9=AERA Petition, 10=AERA Appeal, 11=Cyber Appeal
- **Identifier:** `{casetype, caseno, caseyear}` (Case-No) — no CNR-style single token
- **Open (needs live query):** result-HTML→`Case` map; the Diary-No variant fields (`dairyNo`/`dairyyear` [sic "dairy"]) are only inferred from `goFinal()` JS — re-fetch `casestatus.php` with `pet_type=2` to confirm; verify no-match response shape.

## DRT / DRAT — verdict: partial ⚠ two assumptions overturned
- **Base:** `https://cis.drt.gov.in/drtlive/caseenowisesearch.php`
- **Transport (KEY FINDING):** the `<form>`s have **no action and are never submitted** — every "Search" button is `type=button` with an onclick that fires an **`XMLHttpRequest` GET** to `partyDetail.php`. So transport = `GET https://cis.drt.gov.in/drtlive/partyDetail.php?caseNo=<n>&caseType=<code>&year=<yyyy>&sc=<schema>&id=casetypewise`
- **CSRF:** none · **Captcha:** none in the static form (confirm on the `partyDetail.php` response) ✓
- **Location codes (KEY FINDING — ALL 44 are STATIC in the HTML, NOT AJAX-cascaded):** `schemaname` select carries 5 DRAT (`allahabaddrat`, `chennaidrat`, `delhidrat`, `kolkatadrat`, `mumbaidrat`) + 39 DRT (`delhi`, `mumbai`, …) `value=label` pairs. Same option set is reused in the diary/party/advocate selects.
- **Case-type sets differ DRT vs DRAT** (pick by `sc`): `case_type` (39 DRT schemas, 21 codes: 1=Original Application, 2=Review, 3=Misc, 4=Appeal, 5=URA, 6=Transfer Application, …); `case_type_drat` (5 DRAT schemas, 12 codes: 1=Original Application, 4=Regular Appeal, …). Labels carry a trailing hyphen literally.
- **Identifier:** `{sc (location), caseType (from the right set), caseNo, year}`
- **Open (needs live query):** live `partyDetail.php` GET with a real case → result fragment→`Case`; the `Misdetailreport.php?no=<id>` popup for orders/detail; confirm `cis.drt.gov.in` reachable + no captcha on the result from our prod IP (note: `drt.etribunals.gov.in` geo-refuses us — `cis.` host is the reachable one). Diary/Party/Advocate result fragments uncaptured.

## CESTAT — verdict: partial (search needs captcha; deep-read is captcha-free)
- **Base:** `https://cestat.gov.in/casestatus`
- **Search transport:** `POST` → `casestatus` (relative), urlencoded
- **CSRF:** `csrf_token` (32-hex; hidden input + `<meta csrf_token>`/`<meta X-CSRF-TOKEN>`; re-scrape per session) · **Captcha:** YES — `<img src="/captcha?rand=<float>">`, 6-char alnum, `captcha_code` field (→ ddddocr on the search path)
- **Fields:** `csrf_token`, `schema_type` (bench), `app_type` (`dno`=Diary No default / `cno`=Case No / `pno`=Party / `ino`=O-I-A/O-I-O), `token_no`, `token_year`, `captcha_code`, `button1=SEARCH`
- **Benches (9):** delhi, chandigarh, mumbai, ahmedabad, bangalore, allahabad, kolkata, chennai, hyderabad
- **Captcha-free deep-read:** `validate()` sets the form action to `casedetailreport` — read a KNOWN case without a captcha. ⚠ the exact `casedetailreport/<20-digit-id>/<bench>` URL shape is NOT literal in the HTML (inferred from prior research) — confirm the method + id source live.
- **Open (needs live query):** captcha-solved search POST → result map + where the 20-digit internal id comes from (Action link); confirm the `casedetailreport` read contract (method, no csrf/captcha, id format); how `app_type≠dno` re-renders fields; captcha↔POST session-cookie binding (shared cookie jar).

## NCLAT — verdict: ✅ confirmed (fields_verified = true)
- **Base:** `https://nclat.nic.in/display-board/cases`
- **Transport:** `POST` → `display-board/cases_details` (jQuery `serializeArray`, urlencoded; likely needs `X-Requested-With: XMLHttpRequest`)
- **CSRF:** `_token` (Laravel; hidden input + `<meta csrf-token>`, same value; bound to the session cookie from the GET of `/display-board/cases`; re-scrape each session, sent as a form field) · **Captcha:** none ✓
- **Fields:** `_token`, `search_by` (e.g. `case_no_wise`), `location` (`delhi`=New Delhi, `chennai`), `case_type` (numeric code), `case_number`, `case_year` (`All`/2026…2018), `exact_search_word`, `case_status` (`all`/`P`/`D`), `select_party`, `party_name`, `diary_no`, `advocate_name`
- **`case_type` codes (11):** 32=Company Appeal(AT), 33=Company Appeal(AT)(Ins), 34=Competition Appeal(AT), 35=Interlocutory Application, 36=Compensation Application, 37=Contempt Case(AT), 38=Review Application, 39=Restoration Application, 40=Transfer Appeal, 61=Transfer Original Petition (MRTP-AT)
- **Identifier:** `{search_by, location, case_type, case_number, case_year}`
- **Open (needs live query):** live `cases_details` POST → JSON schema; the per-case 10-section detail endpoint lives in `display-board/js/case_status.js` (not captured — fetch + RE it); confirm the session cookie + `X-Requested-With`/`Accept: application/json` are required; confirm no rate-limit/geo block on `cases_details` from our prod IP.

---

---

## Response shapes — CONFIRMED with real public cases (2026-07-06, live from prod egress)

Live queries fired against publicly-listed cases (no speculative enumeration; the
raw responses are saved as adapter fixtures in the session scratchpad
`resp_*.{json,html}`). This closes the response→`Case` mapping for the three
captcha-free forums; CESTAT's response still needs a captcha-solved search (Dep-B).

### NCLAT — ✅ fully mapped (clean JSON, two-hop)
Sample: **Company Appeal(AT)(Ins) 1/ND/2023** — *Roots Developers Pvt Ltd vs Rajesh Kumar Parakh* (Disposed).
1. **Search** `POST display-board/cases_details` → DataTables JSON `{"data":[[serial, filing_no, case_label, parties_title, filing_date, status_html, view_button(data-filing_no)]]}`. `filing_no` (e.g. `9910110081912022`) is the internal id.
2. **Detail** `POST display-board/view_details` with `search_type=view_details&filing_no=<id>&bench_name=<loc>&_token=<meta>` → `{status,msg,data:{...}}` where `data` has: `case_details[0]{filing_no,status(D/P),case_no,case_year,case_type,date_of_filing,registration_date}`, `party_details{applicant_name[],respondant_name[]}`, `legal_representative`, `first/last/next_hearing_details{court_no,hearing_date,stage_of_case,coram}`, `case_history[]`, `order_history[]`, `ias_other_application[]`, `connected_cases[]`. → maps to the full `Case` (title=applicants vs respondants, court="NCLAT <bench>", stage=stage_of_case, next_hearing_date=next_hearing_details.hearing_date, judge=coram, orders=order_history).

### DRT / DRAT — ✅ fully mapped (HTML, two-hop)
Sample: **OA/1/2023 @ Delhi DRT** (diary 6649/2022, filed 27/12/2022; respondent *M/S Advance Steel Rolls*).
1. **Search** `GET partyDetail.php?caseNo&caseType&year&sc&id=casetypewise` → HTML table row: Diary No, Application Type, Application No, Presented By, Date of Presentation, Applicant, Respondent, + a `MORE DETAIL` link `javascript:popsurety_detailreport('<b64>')`.
2. **Detail** `GET Misdetailreport.php?no=<b64>` (the `<b64>` = base64 of `<internal-id>/<schema>`, **taken verbatim from the search link — not constructed**) → CASE STATUS page: Diary no/Year, Date of Filing, Case Status, Next Listing Purpose, PETITIONER/APPLICANT DETAIL (name/address/advocate), RESPONDENTS DETAIL (name/address/advocate), Purpose/Pleading-Stage rows (hearing history). BeautifulSoup label→value. (Orders: confirm the daily-order surface on a case that has them.)

### TDSAT — ✅ fully mapped (single HTML status page)
Sample: **Telecom Petition 1/2023** — *Reliance Jio Infocomm Ltd vs Union of India* (→ diary 8/2023).
`POST checkhomedetail1.php` returns ONE rich CASE STATUS page (no second hop for the summary): Date of Filing, Case Status, Date of Disposal, Disposal Nature, Filed By, PETITIONER DETAIL (name + advocate), RESPONDENTS DETAIL (name + advocate), and inline proceeding rows (e.g. "Reply filed by Respondent", "Rejoinder filed by Petitioner" — the hearing/pleading history). Orders via `daily_order_view.php?filing_no=<cfy>` popup. BeautifulSoup label→value + the proceedings table.

### CESTAT — request confirmed, response deferred to Wave-1 (Dep-B)
The search needs a solved `captcha_code`; map its response once ddddocr is wired. The `casedetailreport/<id>/<bench>` deep-read (captcha-free) is the refresh path once an id is captured from a solved search.

## Take-aways for T3 Wave-0 build
- **Captcha-free (green-light) request paths:** TDSAT (POST), DRT/DRAT (XHR GET), NCLAT (POST + Laravel `_token`), and CESTAT **deep-read** (`casedetailreport`, once an id is known). Only **CESTAT search-by-number** needs the captcha (ddddocr) — so CESTAT is "soft Dep-B" for the add-by-number step but not for refresh-by-id.
- **No India proxy needed** — all four reachable + HTTP 200 from the prod DO egress (re-confirms the plan's Dep-A elimination).
- **Session handling the adapters must implement:** CESTAT + NCLAT need a GET-then-POST that scrapes a per-session token (`csrf_token` / `_token`) and carries the session cookie; DRT/TDSAT are stateless.
- **Remaining before coding each adapter:** one live query per target with a real case number to capture the response→`Case` mapping (the only open item for all four). An advocate-supplied real case number per forum, or a known public case, unblocks this immediately.
