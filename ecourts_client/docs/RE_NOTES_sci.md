# Supreme Court mobile-API — RE notes (`com.nic.sciapp`)

Reverse-engineered 2026-07 from the `com.nic.sciapp` APK (native + WebView hybrid;
single `classes.dex`, obfuscated; assets/html + jQuery; **no crypto** — plain
Volley requests). Backend: **`https://scourtapp.sci.gov.in/`** (separate from the
captcha-gated `www.sci.gov.in`), routed by `?pageid=<code>&token=<T>`.

## Transport
- **Envelope:** JSON `{data, error}` for data endpoints; **case-status returns
  server-rendered HTML** (a `<table>` of label→value rows).
- **Auth:** every endpoint needs a non-empty `token`. It is a **session token
  (`login_token_id`)** minted by a mobile **OTP login** on a device and persisted
  in the app's `UserCredsPref`. NOT anonymous, NOT mintable server-side (the
  fresh-app bootstrap is device/attestation-gated). Empty/invalid token →
  `{"error":"Permission denyyy!"}`.
- A **captured** token works from any IP (no network-layer device binding) — so a
  token grabbed from the app (adb logcat / HTTPS proxy) drives server-side fetches.
  Capture: `adb logcat` while using the app prints `RequestURL:`/`View Case URL:`
  lines with the live `token`.

## Case-status (implemented)
`GET /?pageid=030001&token=<T>&d_no=<diaryNo>&d_yr=<diaryYr>` → HTML case detail.
Parsed labels → `Case`: `Diary No.` (+ `Filed on <date>`), `Case No.` (SLP...),
`Present/Last Listed On` (date + `[bench]`→judge), `Status/Stage`, `Disp.Type`,
`Category`, `Petitioner(s)`/`Respondent(s)`, `Pet./Resp. Advocate(s)`.
Identifier = `"<diaryNo>:<diaryYr>"`; `cnr`/`forum_case_ref` = `<diaryNo>/<diaryYr>`.

## Other endpoints (mapped, not yet implemented)
- Orders/judgments: `?pageid=130006…&action=get-orders&selected_day=<day>` +
  ROP/officereport PDFs at `scourtapp.sci.gov.in/supremecourt/<yr>/<dno>/…`.
- Case-no search: `&cn=&cy=&ct=`; party search: `&party_type=&partyname=&partyyear=`.
- Login (for reference): send-OTP `sendtoServer(mobile)` → `?pageid=010003` body
  `mobile=`; verify `?pageid=010003` body `mobile=&mvalidatekey=<otp>&logintype=`.

## Production token management
The adapter reads the token from the **`SC_MOBILE_TOKEN`** env var. It expires
(TTL unknown) → on `SCTokenInvalid` re-capture from the app and update the env.
Low-volume forum → periodic manual refresh is acceptable; programmatic re-login
would require cracking the device bootstrap (not done).
