"""GSMA Open Gateway / CAMARA SIM Swap provider adapter.

Pinned spec: CAMARA SimSwap API v2.1.0, released in camaraproject/SimSwap
repository releases r3.2 and r3.3 (latest public: r3.3; r3.3 is a maintenance
release with no spec change since r3.2). Based on Commonalities v0.6.0 and
IdentityAndConsentManagement v0.4.0. Server path template in the spec:
``{apiRoot}/sim-swap/v2`` with operations ``POST /check`` and
``POST /retrieve-date``.

Implements the documented CAMARA SimSwap HTTP integration:
  POST {OG_API_BASE_URL}/check          {phoneNumber?, maxAge} -> {swapped: bool}
  POST {OG_API_BASE_URL}/retrieve-date  {phoneNumber?}        -> {latestSimChange, monitoredPeriod?}

Version tolerance: some deployments expose SimSwap v1.0.0
(camaraproject/SimSwap r1.4, ``{apiRoot}/sim-swap/v1``) instead of v2.1.0.
The two versions get SEPARATE adapter classes below — never silently mixed:

- ``GsmaSandboxProvider``: strict v2.1.0 (``MUST NOT`` phone with 3-legged
  tokens; ``MISSING/UNNECESSARY_IDENTIFIER``, ``OUT_OF_RANGE``,
  ``IDENTIFIER_NOT_FOUND``, ``monitoredPeriod``).
- ``SimSwapV1Provider``: lenient v1.0.0 exactly as your portal Swagger shows
  (``{phoneNumber, maxAge}`` / ``{phoneNumber}``; portal error vocabulary
  ``INVALID_ARGUMENT``, ``UNAUTHENTICATED``, ``PERMISSION_DENIED``,
  ``NOT_FOUND``, ``NOT_SUPPORTED``, ``INTERNAL``, ``UNAVAILABLE``, ``TIMEOUT``;
  no ``monitoredPeriod``; phone optional with 3-legged tokens).

``build_provider`` selects by OG_SIM_SWAP_API_VERSION (default v2). v2 behavior
is unchanged by the v1 addition.

OG_API_BASE_URL MUST be copied verbatim from the sandbox portal and MUST
already include the versioned path (``/sim-swap/v1`` or ``/sim-swap/v2``).
No host, version suffix, token endpoint, or scope is assumed here.

Auth flows (configurable via OG_AUTH_FLOW):
  - mock:               no HTTP, deterministic in-memory responses for dev/tests.
  - bearer:             portal-issued bearer token read ONLY from the local
                        OG_SIM_SWAP_BEARER_TOKEN env var. No token endpoint, no
                        client authentication, no scope request — the token is
                        sent verbatim as ``Authorization: Bearer <token>`` and is
                        never printed, logged, or committed.
  - client_credentials: OAuth2 2-legged. phoneNumber MUST be sent.
  - ciba:               OIDC CIBA 3-legged. phoneNumber MUST NOT be sent in API
                        body (identified from access token); login_hint=tel:<phone>
                        is used at bc-authorize time.
  - auth_code / jwt_bearer: treated as 3-legged (token supplied externally or
                        via token_url); phoneNumber omitted. Full JWT assertion
                        signing is out of scope for the MVP and documented.

No credentials, tokens, auth headers, or full phone numbers are ever logged.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import Settings
from app.schemas import mask_phone
from app.services.providers.base import (
    CheckOutcome,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    RetrieveDateOutcome,
    SimSwapProvider,
    UnsupportedCapability,
)

log = logging.getLogger(__name__)

THREE_LEGGED_FLOWS = {"ciba", "auth_code", "jwt_bearer", "bearer"}
TWO_LEGGED_FLOWS = {"client_credentials"}
# Token-bound flows whose identifier comes from the access token (v2 strict).
TOKEN_BOUND_FLOWS = ("auth_code", "jwt_bearer", "bearer")


def _parse_camara_datetime(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ProviderBadResponse("latestSimChange must be a string or null")
    try:
        # CAMARA uses RFC3339; Python 3.11 fromisoformat handles +HH:MM but not 'Z'.
        candidate = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as exc:
        raise ProviderBadResponse(f"Invalid latestSimChange datetime: {raw}") from exc


def _basic_auth(client_id: str, client_secret: str) -> str:
    # NOTE: CAMARA Security & Interoperability profile mandates private_key_jwt
    # for client authentication. HTTP Basic here is a sandbox convenience used
    # by several aggregator portals (e.g. Telefonica samples) and MUST be
    # confirmed against the selected sandbox's instructions before live use.
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _new_correlator() -> str:
    return uuid.uuid4().hex


class MockSimSwapProvider(SimSwapProvider):
    """Deterministic mock for dev/tests. No network. Live = unconfigured."""

    name = "mock"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        # Numbers ending in ...0000 simulate a recent swap; everything else = no swap.
        self._settings = settings

    async def check(self, *, phone_number: Optional[str], max_age_hours: int) -> CheckOutcome:
        phone = (phone_number or "")
        swapped = phone.endswith("0000")
        return CheckOutcome(swapped=swapped, max_age_hours=max_age_hours)

    async def retrieve_date(
        self, *, phone_number: Optional[str]
    ) -> RetrieveDateOutcome:
        from datetime import timedelta

        phone = (phone_number or "")
        if phone.endswith("0000"):
            # Recent swap (24h ago) so mock check + retrieve-date agree.
            return RetrieveDateOutcome(
                latest_sim_change=datetime.now(timezone.utc) - timedelta(hours=24),
                monitored_period_days=None,
            )
        return RetrieveDateOutcome(latest_sim_change=None, monitored_period_days=120)


class GsmaSandboxProvider(SimSwapProvider):
    """Live CAMARA SimSwap v2.1.0 HTTP integration. Requires real sandbox configuration."""

    name = "gsma-sandbox"
    api_version = "v2"
    # Documented v2.1.0 error vocabulary (codes preserved in messages).
    documented_errors = frozenset(
        {
            "INVALID_ARGUMENT",
            "OUT_OF_RANGE",
            "UNAUTHENTICATED",
            "PERMISSION_DENIED",
            "NOT_FOUND",
            "IDENTIFIER_NOT_FOUND",
            "SERVICE_NOT_APPLICABLE",
            "MISSING_IDENTIFIER",
            "UNNECESSARY_IDENTIFIER",
            "QUOTA_EXCEEDED",
            "TOO_MANY_REQUESTS",
        }
    )

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.name = settings.provider or "gsma-sandbox"

    # -- client handling -------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                verify=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # -- guards -----------------------------------------------------------
    def _require_live(self, *, needs_phone: bool, phone_number: Optional[str]) -> None:
        if self.settings.auth_flow == "mock":
            raise ProviderNotConfigured(
                "Live GSMA integration is unconfigured (OG_AUTH_FLOW=mock). "
                "Supply OG_CLIENT_ID/OG_CLIENT_SECRET/OG_API_BASE_URL/OG_TOKEN_URL/"
                "OG_SIM_SWAP_SCOPE and set OG_AUTH_FLOW."
            )
        if self.settings.missing_config:
            raise ProviderNotConfigured(
                "Missing live configuration: " + ", ".join(self.settings.missing_config)
            )
        if needs_phone and not phone_number:
            # v2.1.0: phone number identifies the subject of 2-legged calls
            # (missing -> 422 MISSING_IDENTIFIER).
            raise ProviderBadResponse(
                "phone_number is required for 2-legged (client_credentials) flow "
                "(CAMARA v2 422 MISSING_IDENTIFIER)."
            )
        if self.settings.auth_flow in TOKEN_BOUND_FLOWS and phone_number:
            # v2.1.0 (strict): phoneNumber MUST NOT be provided with token-bound
            # 3-legged flows -> 422 UNNECESSARY_IDENTIFIER.
            raise ProviderBadResponse(
                "phone_number MUST NOT be provided for token-bound 3-legged flows "
                "(auth_code/jwt_bearer/bearer on v2); it is identified from the "
                "access token (CAMARA v2 422 UNNECESSARY_IDENTIFIER)."
            )

    def _is_three_legged(self) -> bool:
        return self.settings.auth_flow in THREE_LEGGED_FLOWS

    @staticmethod
    def _validate_max_age(max_age_hours: Any) -> None:
        # Fail fast before any token/API call: CreateCheckSimSwap.maxAge is an
        # integer 1..2400 in both v1 and v2 (spec default 240 when omitted;
        # this app always sends it explicitly).
        if (
            not isinstance(max_age_hours, int)
            or isinstance(max_age_hours, bool)
            or not 1 <= max_age_hours <= 2400
        ):
            raise ProviderBadResponse(
                f"maxAge {max_age_hours!r} outside CreateCheckSimSwap range 1..2400."
            )

    # -- token acquisition -------------------------------------------------
    async def _get_access_token(self, phone_number: Optional[str]) -> str:
        now = time.time()
        if self._cached_token and now < self._token_expires_at - 30:
            return self._cached_token
        flow = self.settings.auth_flow
        if flow != "bearer" and not self.settings.sim_swap_scope:
            raise ProviderNotConfigured(
                "OG_SIM_SWAP_SCOPE is empty. Copy the exact scope(s) from the "
                "sandbox portal for SIM Swap (do not guess)."
            )
        previous_expiry = self._token_expires_at
        if flow == "bearer":
            # Portal-issued token: use verbatim from the environment. No token
            # endpoint call is made here — none is implemented or guessed.
            # Read fresh on every call (never cached): a rotated/revoked token
            # must take effect without restarting the process.
            token = self.settings.bearer_token
            if not token:
                raise ProviderNotConfigured(
                    "OG_SIM_SWAP_BEARER_TOKEN is empty. Export the portal-issued "
                    "bearer token locally; it is never hardcoded or logged."
                )
            return token
        elif flow == "client_credentials":
            token = await self._token_client_credentials()
        elif flow == "ciba":
            token = await self._token_ciba(phone_number)
        elif flow in ("auth_code", "jwt_bearer"):
            raise ProviderNotConfigured(
                f"Auth flow '{flow}' requires an externally acquired 3-legged "
                "access token / JWT assertion which is not configured in this MVP. "
                "Use OG_AUTH_FLOW=bearer (portal-issued token), ciba, or "
                "client_credentials."
            )
        else:
            raise ProviderNotConfigured(f"Unknown OG_AUTH_FLOW: {flow!r}")
        self._cached_token = token
        # _token_* methods set _token_expires_at from expires_in when present;
        # only fall back to a conservative default when they did not.
        if self._token_expires_at == previous_expiry:
            self._token_expires_at = now + 300
        return token

    async def _token_client_credentials(self) -> str:
        client = self._get_client()
        try:
            resp = await client.post(
                self.settings.token_url,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": _basic_auth(
                        self.settings.client_id, self.settings.client_secret
                    ),
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": self.settings.sim_swap_scope,
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Token request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Token request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderAuthError("Token endpoint rejected client credentials.")
        if resp.status_code == 429:
            raise ProviderRateLimited()
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Token endpoint error HTTP {resp.status_code}.")
        if resp.status_code != 200:
            raise ProviderAuthError(f"Token endpoint returned HTTP {resp.status_code}.")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadResponse("Token endpoint returned invalid JSON.") from exc
        token = payload.get("access_token")
        if not token:
            raise ProviderBadResponse("Token response missing access_token.")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            self._token_expires_at = time.time() + float(expires_in)
        return str(token)

    async def _token_ciba(self, phone_number: Optional[str]) -> str:
        if not phone_number:
            raise ProviderBadResponse(
                "phone_number is required to start CIBA (used as login_hint)."
            )
        if not self.settings.bc_authorize_url:
            raise ProviderNotConfigured("OG_BC_AUTHORIZE_URL is required for CIBA flow.")
        client = self._get_client()
        auth = _basic_auth(self.settings.client_id, self.settings.client_secret)
        try:
            bc_resp = await client.post(
                self.settings.bc_authorize_url,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": auth,
                },
                data={
                    "login_hint": f"tel:{phone_number}",
                    "scope": self.settings.sim_swap_scope,
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"CIBA authorize timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"CIBA authorize failed: {exc}") from exc
        if bc_resp.status_code in (401, 403):
            raise ProviderAuthError("CIBA authorize rejected client credentials.")
        if bc_resp.status_code == 429:
            raise ProviderRateLimited()
        if bc_resp.status_code >= 400:
            raise ProviderAuthError(
                f"CIBA authorize returned HTTP {bc_resp.status_code}."
            )
        try:
            bc_payload = bc_resp.json()
        except ValueError as exc:
            raise ProviderBadResponse("CIBA authorize returned invalid JSON.") from exc
        auth_req_id = bc_payload.get("auth_req_id")
        if not auth_req_id:
            raise ProviderBadResponse("CIBA authorize missing auth_req_id.")
        # Poll mode (OIDC CIBA Core, poll): the sandbox may approve
        # asynchronously. This MVP performs a SINGLE immediate poll and surfaces
        # pending/denied as ProviderAuthError. This has NOT been verified against
        # the live GSMA sandbox (no credentials, no live calls); confirm polling
        # interval, retries, and grant-type spelling with the portal before live use.
        try:
            tok_resp = await client.post(
                self.settings.token_url,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": auth,
                },
                data={
                    "grant_type": "urn:openid:params:grant-type:ciba",
                    "auth_req_id": auth_req_id,
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"CIBA token poll timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"CIBA token poll failed: {exc}") from exc
        if tok_resp.status_code in (401, 403, 400):
            raise ProviderAuthError(
                "CIBA token poll not authorized (user may not have approved, "
                f"HTTP {tok_resp.status_code})."
            )
        if tok_resp.status_code == 429:
            raise ProviderRateLimited()
        if tok_resp.status_code >= 500:
            raise ProviderUnavailable(
                f"CIBA token endpoint error HTTP {tok_resp.status_code}."
            )
        try:
            tok_payload = tok_resp.json()
        except ValueError as exc:
            raise ProviderBadResponse("CIBA token returned invalid JSON.") from exc
        token = tok_payload.get("access_token")
        if not token:
            raise ProviderBadResponse("CIBA token response missing access_token.")
        return str(token)

    # -- API calls ----------------------------------------------------------
    async def _post_api(
        self, path: str, token: str, body: dict, correlator: str
    ) -> httpx.Response:
        client = self._get_client()
        # rstrip: OG_API_BASE_URL may carry a trailing slash from the portal;
        # paths below start with "/". Guarantees no "//" or duplicated segments.
        url = f"{self.settings.api_base_url.rstrip('/')}{path}"
        try:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-correlator": correlator,
                },
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"SIM Swap API timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"SIM Swap API request failed: {exc}") from exc
        return resp

    @staticmethod
    def _api_code_and_message(resp: httpx.Response) -> tuple[str, str]:
        try:
            payload = resp.json()
        except Exception:
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        code = str(payload.get("code", ""))
        message = str(payload.get("message", ""))
        return code, message

    def _raise_for_api_status(self, resp: httpx.Response) -> None:
        code = resp.status_code
        if code == 429:
            raise ProviderRateLimited("SIM Swap API rate limit exceeded.")
        if code in (401, 403):
            api_code, message = self._api_code_and_message(resp)
            # v1: 403 PERMISSION_DENIED or INVALID_TOKEN_CONTEXT (phone/token
            # mismatch). v2: 403 PERMISSION_DENIED only. Both stay auth failures
            # (unknown, never negative) with the code preserved for diagnosis.
            raise ProviderAuthError(
                f"SIM Swap API rejected access token. {api_code} {message}".strip()
            )
        if code == 404:
            api_code, message = self._api_code_and_message(resp)
            raise ProviderUnavailable(
                f"SIM Swap API: subscription not found. {api_code} {message}".strip()
            )
        if code == 400:
            api_code, message = self._api_code_and_message(resp)
            # v2.1.0 enumerates INVALID_ARGUMENT and OUT_OF_RANGE (operator
            # retention, e.g. "could not exceed local regulations (30 days)").
            raise ProviderBadResponse(
                f"SIM Swap API rejected request. {api_code} {message}".strip()
            )
        if code == 422:
            api_code, message = self._api_code_and_message(resp)
            raise ProviderBadResponse(
                f"SIM Swap identifier error. {api_code} {message}".strip()
            )
        if code >= 500:
            # Both versions: 500 INTERNAL, 503 UNAVAILABLE, 504 TIMEOUT (v1 names)
            # or Commonalities equivalents. Preserve code+message for diagnosis;
            # always unavailable (unknown), never negative.
            api_code, message = self._api_code_and_message(resp)
            raise ProviderUnavailable(
                f"SIM Swap API error HTTP {code}. {api_code} {message}".strip()
            )
        if code != 200:
            raise ProviderBadResponse(f"SIM Swap API returned HTTP {code}.")

    def _build_check_body(
        self, *, phone_number: Optional[str], max_age_hours: int
    ) -> dict[str, Any]:
        # CreateCheckSimSwap (v1/v2 identical): maxAge integer 1..2400.
        self._validate_max_age(max_age_hours)
        body: dict[str, Any] = {"maxAge": max_age_hours}
        # v2.1.0: phoneNumber MUST NOT be sent with 3-legged tokens.
        # CIBA still needs phone_number for login_hint (used at token time).
        if phone_number and not self._is_three_legged():
            body["phoneNumber"] = phone_number
        return body

    def _build_retrieve_body(self, *, phone_number: Optional[str]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if phone_number and not self._is_three_legged():
            body["phoneNumber"] = phone_number
        return body

    async def check(self, *, phone_number: Optional[str], max_age_hours: int) -> CheckOutcome:
        self._validate_max_age(max_age_hours)
        three_legged = self._is_three_legged()
        self._require_live(needs_phone=not three_legged, phone_number=phone_number)
        token = await self._get_access_token(phone_number)
        body = self._build_check_body(
            phone_number=phone_number, max_age_hours=max_age_hours
        )
        correlator = _new_correlator()
        resp = await self._post_api("/check", token, body, correlator)
        self._raise_for_api_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadResponse("SIM Swap /check returned invalid JSON.") from exc
        if "swapped" not in payload or not isinstance(payload["swapped"], bool):
            raise ProviderBadResponse("SIM Swap /check missing boolean 'swapped'.")
        log.info(
            "sim-swap check completed masked_phone=%s maxAge=%s swapped=%s",
            mask_phone(phone_number),
            max_age_hours,
            payload["swapped"],
        )
        return CheckOutcome(
            swapped=bool(payload["swapped"]),
            max_age_hours=max_age_hours,
            x_correlator=correlator,
            http_status=resp.status_code,
        )

    async def retrieve_date(
        self, *, phone_number: Optional[str]
    ) -> RetrieveDateOutcome:
        three_legged = self._is_three_legged()
        self._require_live(needs_phone=not three_legged, phone_number=phone_number)
        token = await self._get_access_token(phone_number)
        body = self._build_retrieve_body(phone_number=phone_number)
        correlator = _new_correlator()
        resp = await self._post_api("/retrieve-date", token, body, correlator)
        self._raise_for_api_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadResponse(
                "SIM Swap /retrieve-date returned invalid JSON."
            ) from exc
        if "latestSimChange" not in payload:
            raise ProviderBadResponse(
                "SIM Swap /retrieve-date missing 'latestSimChange'."
            )
        latest = _parse_camara_datetime(payload.get("latestSimChange"))
        monitored = payload.get("monitoredPeriod")
        if monitored is not None and not isinstance(monitored, int):
            raise ProviderBadResponse("monitoredPeriod must be an integer or absent.")
        log.info(
            "sim-swap retrieve-date completed masked_phone=%s has_date=%s",
            mask_phone(phone_number),
            latest is not None,
        )
        return RetrieveDateOutcome(
            latest_sim_change=latest,
            monitored_period_days=monitored,
            x_correlator=correlator,
            http_status=resp.status_code,
        )


class SimSwapV1Provider(GsmaSandboxProvider):
    """Live CAMARA SimSwap v1.0.0 adapter (camaraproject/SimSwap r1.4).

    Matches the portal Swagger reference exactly:
      POST {OG_API_BASE_URL}/check          {"phoneNumber", "maxAge"} -> {"swapped"}
      POST {OG_API_BASE_URL}/retrieve-date  {"phoneNumber"}          -> {"latestSimChange"}
    Optional ``x-correlator`` header on both operations (inherited).
    Documented errors: 400 INVALID_ARGUMENT, 401 UNAUTHENTICATED,
    403 PERMISSION_DENIED (+ INVALID_TOKEN_CONTEXT tolerated from the full
    v1 YAML for phone/token mismatch), 404 NOT_FOUND, 422 NOT_SUPPORTED
    (+ UNIDENTIFIABLE_PHONE_NUMBER tolerated), 500 INTERNAL, 503 UNAVAILABLE,
    504 TIMEOUT. No ``monitoredPeriod`` in v1 responses (absent -> None).
    Phone rule is lenient: phoneNumber optional with 3-legged tokens and
    passed through for server-side match validation (never client-rejected).
    """

    name = "gsma-sandbox-v1"
    api_version = "v1"
    documented_errors = frozenset(
        {
            "INVALID_ARGUMENT",
            "UNAUTHENTICATED",
            "PERMISSION_DENIED",
            "INVALID_TOKEN_CONTEXT",
            "NOT_FOUND",
            "NOT_SUPPORTED",
            "UNIDENTIFIABLE_PHONE_NUMBER",
            "INTERNAL",
            "UNAVAILABLE",
            "TIMEOUT",
            "QUOTA_EXCEEDED",
            "TOO_MANY_REQUESTS",
        }
    )

    def _require_live(self, *, needs_phone: bool, phone_number: Optional[str]) -> None:
        if self.settings.auth_flow == "mock":
            raise ProviderNotConfigured(
                "Live GSMA integration is unconfigured (OG_AUTH_FLOW=mock). "
                "Supply OG_CLIENT_ID/OG_CLIENT_SECRET/OG_API_BASE_URL/OG_TOKEN_URL/"
                "OG_SIM_SWAP_SCOPE and set OG_AUTH_FLOW."
            )
        if self.settings.missing_config:
            raise ProviderNotConfigured(
                "Missing live configuration: " + ", ".join(self.settings.missing_config)
            )
        if needs_phone and not phone_number:
            # v1.0.0: phoneNumber MUST be provided for 2-legged calls.
            raise ProviderBadResponse(
                "phone_number is required for 2-legged (client_credentials) flow "
                "(CAMARA v1: phoneNumber MUST be provided)."
            )
        # v1 lenient: never reject phoneNumber for 3-legged flows here; the
        # server validates match (mismatch -> 403) or identifiability (422).

    def _build_check_body(
        self, *, phone_number: Optional[str], max_age_hours: int
    ) -> dict[str, Any]:
        # Same CreateCheckSimSwap range as v2 (v1 source of truth: 1..2400).
        self._validate_max_age(max_age_hours)
        body: dict[str, Any] = {"maxAge": max_age_hours}
        if phone_number:
            body["phoneNumber"] = phone_number
        return body

    def _build_retrieve_body(self, *, phone_number: Optional[str]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if phone_number:
            body["phoneNumber"] = phone_number
        return body


def build_provider(
    settings: Settings, client: Optional[httpx.AsyncClient] = None
) -> SimSwapProvider:
    """Factory: mock unless live credentials + non-mock flow are configured.

    Selects the SEPARATE v1 or v2 adapter by OG_SIM_SWAP_API_VERSION —
    paths and schemas are never mixed within one class. The "simulation"
    flow returns the stateful temporary-data provider (offline, volatile).
    """
    from app.services.providers.simulation import SimulatedSimSwapProvider

    if settings.auth_flow == "simulation":
        sim = SimulatedSimSwapProvider(settings)
        sim.name = settings.provider or "simulation"
        return sim
    if settings.auth_flow == "mock" or not settings.is_live_configured:
        mock = MockSimSwapProvider(settings)
        mock.name = settings.provider or "mock"
        return mock
    if settings.is_v1:
        return SimSwapV1Provider(settings, client=client)
    return GsmaSandboxProvider(settings, client=client)


__all__ = [
    "GsmaSandboxProvider",
    "SimSwapV1Provider",
    "MockSimSwapProvider",
    "ProviderError",
    "build_provider",
]
