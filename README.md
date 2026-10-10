# Cybersafe — SIM Swap Detection (GSMA Open Gateway / CAMARA)

AI-Powered Mobile Cybercrime Prevention & Scam Protection — hackathon MVP, backend slice.

## 1. What the SIM Swap API does and does not do

Pinned spec: **CAMARA SimSwap API v2.1.0**, released in **camaraproject/SimSwap
repository releases r3.2 and r3.3 (latest public: r3.3)** — r3.3 is a
maintenance release with no spec change since r3.2. Based on Commonalities
v0.6.0 and IdentityAndConsentManagement v0.4.0. Server path template in the
spec: `{apiRoot}/sim-swap/v2` with `POST /check` and `POST /retrieve-date`.
(Corrects the earlier inconsistent "r2.2 / v2.1.0" label: r2.2 predates v2.1.0.)

Source of truth for schemas: the **SIM Swap v1 portal Swagger reference**
(`SimSwapInfo.latestSimChange` nullable RFC 3339 with timezone,
`CheckSimSwapInfo.swapped` boolean, `PhoneNumber` `^\+[1-9][0-9]{4,14}$`,
`CreateCheckSimSwap{phoneNumber, maxAge 1..2400 default 240}`,
`CreateSimSwapDate{phoneNumber}`, `ErrorInfo{status, code, message}`, server
template `{apiRoot}/sim-swap/v1`, `POST /check` + `POST /retrieve-date`).
These shapes are identical in v2.1.0 (which adds `monitoredPeriod` and a wider
error vocabulary), so one validation layer serves both; the separate
`SimSwapV1Provider` / `GsmaSandboxProvider` adapters own the per-version rules.

- `POST {OG_API_BASE_URL}/check` with `{phoneNumber?, maxAge}` → `{swapped: bool}`.
  `maxAge` = lookback window in **hours, 1–2400** (spec default 240 when omitted;
  this app defaults `lookback_hours` to **72h** for the MVP and always sends
  `maxAge` explicitly). `400 OUT_OF_RANGE` (v2, with `code`/`message`) or `400
  INVALID_ARGUMENT` (v1) on schema violations — surfaced as `unknown`, never as
  `false`.
- `POST {OG_API_BASE_URL}/retrieve-date` with `{phoneNumber?}` → `{latestSimChange, monitoredPeriod?}`.
  `latestSimChange` is RFC3339 or `null`; `monitoredPeriod` (days, v2 only) means
  "no swap in last N days". `null` **with** `monitoredPeriod` → completed, no swap
  in window. `null` **without** it (always the case for v1, which has no
  `monitoredPeriod`) → `unknown`: operator retention is unknowable, so no recency
  verdict is possible and it is never reported as safe.

What it does: returns a **telco risk signal** (recent SIM change) to add friction
before sensitive actions (OTP, password reset, high-value transfer).

What it does **not** do:
- Prove fraud. A swap can be legitimate (lost SIM, new phone).
- Prove safety. **No recent swap ≠ safe phone.** Other attack vectors remain.
- Identify *who* swapped or *why*.

Three internal outcomes (see `app/services/sim_swap.py`):
`completed+detected` → `recent_sim_change` · `completed+no swap` → `no_recent_change` ·
`unknown/unsupported` → `check_unavailable/unsupported_operation`.
A failed HTTP call **never** becomes `sim_swap_detected=false`.

## 2. Sandbox registration and application creation

1. Go to https://open-gateway.gsma.com/sandbox and **Sign in with GitHub**.
2. Explore APIs → **SIM Swap** → **Swagger API Reference**.
3. Create an application (portal "My Apps" / "Create application").
4. Subscribe that application to **SIM Swap**.
5. Note the portal's **Client ID / Client Secret**, **Token URL**,
   **BC-Authorize URL** (for CIBA), **API base URL verbatim including the
   versioned path**, and required **scope** string(s) (copy verbatim).
   Confirmed for this sandbox (2026-10-09): server
   `https://open-gateway-sandbox.gsma.com`, SIM Swap **v1.0.0**
   (`/sim-swap/v1/check`, `/sim-swap/v1/retrieve-date`), Client Credentials,
   scopes `sim-swap:check` + `sim-swap:retrieve-date` (space-separated single
   value). Token URL and client-auth method are NOT yet verified — see §12.
6. For production you will repeat this on your chosen operator/aggregator
   portal — the sandbox is CAMARA-compliant so the integration ports over.

> The general GSMA sandbox is **not** Telefónica's separate sandbox
> (`developers.opengateway.telefonica.com`). Do not mix their SDK/URLs.

## 3. Where to obtain each configuration value

| Env var | Source |
|---|---|
| `OG_PROVIDER` | Free-form label, e.g. `gsma-sandbox`. |
| `OG_AUTH_FLOW` | You choose: `mock` (default) \| `client_credentials` \| `ciba` \| `auth_code` \| `jwt_bearer`. Sandbox lists Client Credentials, CIBA, Authorization Code, JWT Bearer. |
| `OG_CLIENT_ID` / `OG_CLIENT_SECRET` | Sandbox portal → your application → Credentials. Both required for live (client_secret was previously optional — fixed). |
| `OG_API_BASE_URL` | Portal → SIM Swap API → base URL **verbatim, ending in the deployed version path (`/sim-swap/v1` or `/sim-swap/v2`)**. No host or suffix is invented in this repo. |
| `OG_SIM_SWAP_API_VERSION` | `v2` (default, v2.1.0) or `v1` (v1.0.0). Set only from the portal's deployed path; request shapes are identical, error/strictness handling adapts (see §13). |
| `OG_TOKEN_URL` | Portal → Endpoints → token. |
| `OG_BC_AUTHORIZE_URL` | Portal → Endpoints → bc-authorize (**required** for CIBA; live is blocked without it — fixed). |
| `OG_SIM_SWAP_SCOPE` | Portal → SIM Swap → scope(s), copied verbatim. **No default is assumed** (the previous Telefonica-style default was removed — fixed). |
| `OG_TIMEOUT_SECONDS` / `OG_DEFAULT_LOOKBACK_HOURS` / `OG_MAX_LOOKBACK_HOURS` | Local tuning. Default lookback = 72 (spec max 2400; operator may enforce a lower retention via `400 OUT_OF_RANGE`). |

Auth rules (CAMARA v2.1.0, enforced in code):
- **2-legged** (`client_credentials`): `phoneNumber` **MUST** be sent; missing → `422 MISSING_IDENTIFIER` semantics (our API requires it).
- **3-legged CIBA** (`ciba`): `phoneNumber` is required **for `login_hint=tel:<phone>`** at bc-authorize time but **MUST NOT** appear in the `/check`/`/retrieve-date` body (token-bound). Enforced + tested.
- **Token-bound 3-legged** (`auth_code`/`jwt_bearer`): `phoneNumber` **MUST NOT** be provided at all → rejected as `422 UNNECESSARY_IDENTIFIER` semantics (new guard + test). These flows need an externally acquired token — MVP raises `unconfigured` with guidance.

## 4. Configure environment variables

```powershell
Copy-Item .env.example .env
# edit .env with your values; keep OG_AUTH_FLOW=mock until you have creds
```

Never paste secrets into source code or commit `.env` (already in `.gitignore`).

## 5. Install dependencies

```powershell
python --version   # 3.11+
pip install -r requirements.txt
```

Deps: FastAPI, Uvicorn, Pydantic v2, HTTPX, python-dotenv, pytest, pytest-asyncio.

## 6. Run tests

```powershell
pytest -q
# 125 passed, 1 skipped (mocked HTTP, no live calls; live test skips without OG_LIVE_TEST=1)
```

Opt-in live bearer test (NOT part of the standard suite; makes REAL network
calls only when explicitly enabled — otherwise skipped):

```powershell
$env:OG_AUTH_FLOW="bearer"
$env:OG_SIM_SWAP_API_VERSION="v1"
$env:OG_API_BASE_URL="https://open-gateway-sandbox.gsma.com/sim-swap/v1"
$env:OG_SIM_SWAP_BEARER_TOKEN="<portal-issued-token>"  # session only, never committed
$env:OG_TEST_PHONE_NUMBER="+<E.164>"
$env:OG_LIVE_TEST="1"
pytest tests/test_live_sim_swap.py -q
```

The token travels only in the `Authorization: Bearer` header and is never
printed, logged, or committed. Failure output carries HTTP status +
provider `code`/`message` only. Pass criteria are strict: the test fails
unless BOTH operations return `status="completed"` — `unknown` can never
pass silently, so a pass proves both live calls succeeded.

## 7. Start the FastAPI server

```powershell
uvicorn app.main:app --reload
```

## 8. Test the endpoint through /docs

1. Open http://127.0.0.1:8000/docs.
2. `GET /health` → `{"live_configured": false}` means mock mode (expected pre-registration).
3. `POST /api/security/sim-swap/check` → Try:
   - `{"phone_number": "+346661113334", "lookback_hours": 72}` → `no_recent_change`.
   - `{"phone_number": "+3412340000", "lookback_hours": 72}` → `recent_sim_change` (mock convention: numbers ending `0000` simulate a swap).
4. `POST /api/security/sim-swap/retrieve-date` → `{"phone_number": "+346661113334"}`.
5. Full numbers never appear in responses/logs — only `masked_phone` like `+34******3334`.

## 9. Mock vs live

- **Mock** (default `OG_AUTH_FLOW=mock`, or any missing live value):
  `build_provider()` returns `MockSimSwapProvider`. `/health.live_configured=false`
  and `/health.missing_config` lists exactly what is absent. All 125 tests run offline.
- **Simulation** (`OG_AUTH_FLOW=simulation`): stateful temporary-data double.
  A volatile in-memory registry holds subscribers + swap history; mutate it via
  `/api/simulator/subscribers`, `/api/simulator/swaps`, `/api/simulator/reset`
  (masked phones only) and watch `/check` + `/retrieve-date` react. Nothing is
  persisted; restart reseeds the two demo numbers. Ideal for interactive demos;
  proves integration and UX flow, never real-world accuracy.
- **Live bearer** (`OG_AUTH_FLOW=bearer` + `OG_SIM_SWAP_BEARER_TOKEN` +
  `OG_API_BASE_URL`): `build_provider()` returns the versioned live adapter —
  `SimSwapV1Provider` when `OG_SIM_SWAP_API_VERSION=v1`, otherwise
  `GsmaSandboxProvider` (v2.1.0, unchanged). No token endpoint is called; the
  env token is sent verbatim as `Authorization: Bearer`.
- **Live OAuth** (`client_credentials`/`ciba` + IDs/URLs/scopes): token-endpoint
  flows as before. Real HTTPX calls, TLS verify, timeouts, token caching,
  unique `x-correlator` per request and the actual HTTP status recorded in
  evidence. No live call is made unless you configure it.
- This repo is **mock-only until you supply real sandbox values**. No endpoints,
  credentials, scopes, or success responses are fabricated.

## 10. Known limitations

- **Spec pins:** v2.1.0 (`camaraproject/SimSwap` r3.2/r3.3, Commonalities v0.6.0)
  via `GsmaSandboxProvider`, plus v1.0.0 (`camaraproject/SimSwap` r1.4,
  Commonalities 0.4.0) via the separate `SimSwapV1Provider` — selected only by
  `OG_SIM_SWAP_API_VERSION`, never mixed. v2 codes: `400 INVALID_ARGUMENT`/
  `OUT_OF_RANGE`, `401 UNAUTHENTICATED`, `403 PERMISSION_DENIED`, `404 NOT_FOUND`/
  `IDENTIFIER_NOT_FOUND`, `422 SERVICE_NOT_APPLICABLE`/`MISSING_IDENTIFIER`/
  `UNNECESSARY_IDENTIFIER`, `429 QUOTA_EXCEEDED`/`TOO_MANY_REQUESTS`. v1 portal
  vocabulary (§13): `400 INVALID_ARGUMENT`, `401 UNAUTHENTICATED`, `403
  PERMISSION_DENIED` (+`INVALID_TOKEN_CONTEXT` tolerated), `404 NOT_FOUND`, `422
  NOT_SUPPORTED` (+`UNIDENTIFIABLE_PHONE_NUMBER` tolerated), `500 INTERNAL`, `503
  UNAVAILABLE`, `504 TIMEOUT`. `code`+`message` are preserved on 400/401/403/404/422
  and now also on 5xx; rate limiting, malformed JSON, and timeouts map to
  `unknown`, never to `false`.
- **CIBA honesty:** the adapter does bc-authorize (`login_hint=tel:<phone>` +
  scope) then a SINGLE immediate token poll (`grant_type
  urn:openid:params:grant-type:ciba`). Polling interval/retries, client
  authentication method (code uses HTTP Basic; CAMARA profile mandates
  `private_key_jwt` — confirm with the portal), and scope/purpose spelling are
  **NOT verified against the live sandbox** (no credentials, no live calls).
  Passing unit tests do not prove live auth works.
- **Correlator:** `x-correlator` (spec pattern `^[a-zA-Z0-9-_:;./<>{}]{0,256}$`)
  is generated per API call, sent as a header, and recorded in evidence. Portal
  echo headers are not asserted.
- **Consent/privacy:** SIM Swap processes personal data → normally **3-legged**
  (CIBA/AuthCode); 2-legged only if your provider explicitly allows it for
  fraud-prevention legitimate interest. Handle opt-out, data minimisation,
  masked logging (done), and local retention law.
- **Deployment:** single-shot CIBA poll is MVP; add retry/backoff with polling
  hints, metrics, and secret-manager integration before production. Never retry
  non-idempotent calls blindly.
- **Scope:** SDKs built for one aggregator (e.g. Telefónica) are **not** assumed
  portable — this repo uses direct documented HTTP with portal-verbatim URLs/scopes.

## 11. Audit fixes (this review)

| # | Issue found | Fix |
|---|---|---|
| 1 | Version string "r2.2 / v2.1.0" is inconsistent (v2.1.0 ships in r3.2/r3.3, not r2.2) | Pinned to v2.1.0 @ r3.2/r3.3 in code + README + `.env.example` |
| 2 | Invented example base URL `https://sandbox.open-gateway.gsma.com/sim-swap/v0` (spec: `{apiRoot}/sim-swap/v2`) | Removed; `.env.example` + README require verbatim portal value ending in `/sim-swap/v2` |
| 3 | Hardcoded Telefonica-style default scope assumed portable | Default scope removed (`""`); live blocked + `missing_config` reports `OG_SIM_SWAP_SCOPE` |
| 4 | `client_secret` not required for live (Basic token auth needs it) | Required; `is_live_configured`/`missing_config` updated |
| 5 | CIBA without `bc-authorize` URL passed live check then failed late | `is_live_configured=false` + `missing OG_BC_AUTHORIZE_URL` up front |
| 6 | Token `expires_in` overwritten by 300s default (extra token calls, wrong cache) | Only default when token endpoint gave no `expires_in`; regression test added |
| 7 | Token-bound 3-legged (`auth_code`/`jwt_bearer`) silently dropped a supplied phone instead of 422 | New `UNNECESSARY_IDENTIFIER`-semantics guard + tests |
| 8 | Error bodies discarded (`message` only, no `code`; 404 code lost) | `code`+`message` surfaced (`OUT_OF_RANGE`, `IDENTIFIER_NOT_FOUND` tested) |
| 9 | No `x-correlator` (spec parameter + response header) | Generated per call, sent, recorded in evidence; tested |
| 10 | Tests pinned to `/sim-swap/v0` paths | Updated to `/sim-swap/v2` |
| 11 | Client auth method unverified (Basic vs `private_key_jwt`) | Documented as sandbox-convenience assumption requiring portal confirmation |

## 12. Exact steps to enable live integration

1. Already in local `.env` (non-secret, confirmed 2026-10-09):
   `OG_API_BASE_URL=https://open-gateway-sandbox.gsma.com/sim-swap/v1`,
   `OG_SIM_SWAP_API_VERSION=v1`,
   `OG_SIM_SWAP_SCOPE=sim-swap:check sim-swap:retrieve-date`, `OG_AUTH_FLOW=mock`
   (mock stays default; no live calls). Still blank: `OG_CLIENT_ID`,
   `OG_CLIENT_SECRET`, `OG_TOKEN_URL` — fill from the portal's application /
   token example. Do NOT invent the token URL.
2. Confirm with the portal's token example: token endpoint URL, client
   authentication method (HTTP Basic vs `private_key_jwt`), and exact
   token-request parameters. The adapter currently uses HTTP Basic + form
   `grant_type`/`scope` as a sandbox convenience — unverified until the portal
   example is checked.
3. `GET /health` must show `"live_configured": true, "missing_config": []`
   (bearer: `OG_AUTH_FLOW=bearer` + `OG_SIM_SWAP_BEARER_TOKEN` + `OG_API_BASE_URL`;
   OAuth: `OG_AUTH_FLOW=client_credentials` plus all values above).
4. Only after you explicitly authorize: run the bearer live test (§6 command)
   with `OG_TEST_PHONE_NUMBER` set and verify a real sandbox response.
   An expired/rejected token surfaces as `401 UNAUTHENTICATED` → `unknown`;
   re-export a fresh portal token locally (bearer tokens are never refreshed
   in-app). Swagger example numbers are parsing fixtures, not verified live
   test cases — a `completed` result for one proves connectivity and parsing,
   not real-world detection accuracy.
5. Until step 4 succeeds, live integration is **unverified** — this repo makes no
   live-connection claim.

## 13. Appendix: v1.0.0 document you found vs the v2.1.0 adapter

**Classification: generic API specification, not a live endpoint and not a mock
server.** Evidence:
- `http://localhost:9091` is the `servers.variables.apiRoot.default` placeholder
  in **every** CAMARA SimSwap YAML — v1.0.0 (`{apiRoot}/sim-swap/v1`, r1.4) and
  v2.x (`{apiRoot}/sim-swap/v2`, r2.2/r3.3) alike. It is never a routable GSMA host.
- The doc itself says the server URL is configurable through `apiRoot` with
  description "defined by the service provider" — i.e. you must substitute the
  real portal host. A live sandbox endpoint would be an `https://` remote host
  shown in the portal's server dropdown, not `localhost`.
- `localhost:9091` is not assumed anywhere in this repo and no request is ever
  sent there.

**Inspected (v1.0.0, r1.4, Commonalities 0.4.0 — fetched raw YAML):**
- Security: `openId` with `sim-swap:retrieve-date` / `sim-swap:check` / `sim-swap`
  (identical to v2.1.0); `openIdConnectUrl` is the `https://example.com/...` placeholder.
- Requests: `POST /check {phoneNumber?, maxAge 1–2400 default 240}`, `POST
  /retrieve-date {phoneNumber?}`, E.164 `^+[1-9][0-9]{4,14}$` — identical to v2.1.0.
- Responses 200: `{swapped: bool}` / `{latestSimChange: datetime|null}` — identical
  except **v1 has no `monitoredPeriod`** (added in v2).
- Errors: v1 uses `400 INVALID_ARGUMENT` (no `OUT_OF_RANGE`), `403 PERMISSION_DENIED`
  + **`INVALID_TOKEN_CONTEXT`** (phone/token mismatch; dropped in v2), `404 NOT_FOUND`
  (no `IDENTIFIER_NOT_FOUND`), `422 NOT_SUPPORTED` + **`UNIDENTIFIABLE_PHONE_NUMBER`**
  (replaced in v2 by `SERVICE_NOT_APPLICABLE`/`MISSING_IDENTIFIER`/`UNNECESSARY_IDENTIFIER`),
  no documented `429`, plus explicit `500/503/504`.
- Phone rule: v1 **lenient** ("recommended NOT to include" phone with 3-legged;
  mismatch → `403 INVALID_TOKEN_CONTEXT`, unidentifiable → `422
  UNIDENTIFIABLE_PHONE_NUMBER`). v2 **strict** (`MUST NOT` → `422 UNNECESSARY_IDENTIFIER`,
  missing → `422 MISSING_IDENTIFIER`).

**Adapter compatibility (separate classes, v2 behavior unchanged):**
- `GsmaSandboxProvider` is pure v2.1.0 (strict `MUST NOT` phone rule).
  `SimSwapV1Provider` subclasses it but overrides the phone guard, both body
  builders, and declares the v1 error vocabulary — v1 and v2 paths/schemas are
  never mixed inside one class. `build_provider` picks by `OG_SIM_SWAP_API_VERSION`.
- `OG_API_BASE_URL` carries the full versioned path verbatim (`/sim-swap/v1` or
  `/sim-swap/v2`); the class only appends `/check` or `/retrieve-date`.
- `403`/`422`/`400`/`404` surface `code`+`message` (covering both vocabularies);
  `5xx` now also preserves codes (`INTERNAL`/`UNAVAILABLE`/`TIMEOUT` tested);
  absent `monitoredPeriod` maps to `None` (v1 has none).
- 13 new tests cover: v1/v2 factory selection + vocabulary separation, v1 CIBA
  pass-through, v1 guard leniency, `INVALID_TOKEN_CONTEXT`,
  `UNIDENTIFIABLE_PHONE_NUMBER`, v1 retrieve-date without `monitoredPeriod`,
  portal `NOT_SUPPORTED`, `500`/`503`/`504` code preservation (both adapters),
  v2 strictness retention, and unique correlators on retrieve-date.
- Schema-alignment tests against the supplied v1 reference: portal `/check`
  example exact body (`maxAge: 240` → `swapped: true`), portal retrieve example
  with `+02:00` offset, `maxAge` rejected outside 1..2400 (incl. non-integers,
  fail-fast before any token call), v1 `400 INVALID_ARGUMENT` / `401
  UNAUTHENTICATED` / `403 PERMISSION_DENIED` / `404 NOT_FOUND` code preservation,
  malformed `latestSimChange` rejection, E.164 boundary numbers, and the
  spec-default-240 vs app-default-72 distinction.
  Full suite: **125 passed, 1 skipped**.

**Deprecation note on your portal reference:** the error list you supplied (422
`NOT_SUPPORTED` only, no `INVALID_TOKEN_CONTEXT`/`UNIDENTIFIABLE_PHONE_NUMBER`)
matches the portal's trimmed Swagger rendering, while the full v1.0.0 YAML (r1.4)
additionally documents those two codes. The v1 adapter tolerates both the trimmed
and full vocabularies. `localhost:9091` remains the spec-template default in all
versions — never the remote endpoint (see classification above).

**Version verdict: v1 CONFIRMED as the deployed API (portal details 2026-10-09:
server `https://open-gateway-sandbox.gsma.com`, SIM Swap v1.0.0, Client
Credentials, scopes `sim-swap:check`/`sim-swap:retrieve-date`,
`/sim-swap/v1/check` + `/sim-swap/v1/retrieve-date`).** Local `.env` carries
these non-secret values with `OG_SIM_SWAP_API_VERSION=v1`; offline tests assert
the exact final URLs with no path duplication. **Live integration is still
UNVERIFIED**: token URL, client-auth method, and credentials are missing and no
live request has been made. Do not switch `OG_AUTH_FLOW` off mock until those
are filled from the portal and a live test is explicitly authorized.

## Project structure

```
app/main.py  app/config.py  app/schemas.py
app/api/routes.py
app/services/sim_swap.py
app/services/providers/base.py  app/services/providers/gsma_sandbox.py
tests/test_schemas.py  tests/test_api.py  tests/test_sim_swap.py  tests/test_live_sim_swap.py
.env.example  requirements.txt  pytest.ini  README.md
```
