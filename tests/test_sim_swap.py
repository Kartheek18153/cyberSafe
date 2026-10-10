"""Service + provider tests. All HTTP is mocked; no live calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.services.providers.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SimSwapProvider,
    UnsupportedCapability,
)
from app.services.providers.gsma_sandbox import (
    GsmaSandboxProvider,
    MockSimSwapProvider,
    SimSwapV1Provider,
    build_provider,
)
from app.services.sim_swap import SimSwapService


def mock_settings(**overrides) -> Settings:
    base = Settings(
        provider="gsma-sandbox",
        client_id="",
        client_secret="",
        api_base_url="",
        token_url="",
        bc_authorize_url="",
        sim_swap_scope="",
        auth_flow="mock",
        timeout_seconds=5.0,
        default_lookback_hours=72,
        max_lookback_hours=2400,
        sim_swap_api_version="v2",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def live_settings(**overrides) -> Settings:
    s = mock_settings(**overrides)
    s.auth_flow = overrides.get("auth_flow", "client_credentials")
    s.client_id = overrides.get("client_id", "test-id")
    s.client_secret = overrides.get("client_secret", "test-secret")
    s.api_base_url = overrides.get("api_base_url", "https://sandbox.example/sim-swap/v2")
    s.token_url = overrides.get("token_url", "https://sandbox.example/token")
    s.bc_authorize_url = overrides.get(
        "bc_authorize_url", "https://sandbox.example/bc-authorize"
    )
    if "sim_swap_scope" not in overrides:
        s.sim_swap_scope = "sim-swap:check sim-swap:retrieve-date"
    return s


def mock_transport(handler) -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(_handle)


# -- mock provider semantics --------------------------------------------

async def test_mock_positive_and_negative():
    svc = SimSwapService(MockSimSwapProvider(), mock_settings())
    pos = await svc.check(phone_number="+3412340000", lookback_hours=72)
    assert pos.status == "completed"
    assert pos.sim_swap_detected is True
    assert pos.risk_signal == "recent_sim_change"

    neg = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert neg.status == "completed"
    assert neg.sim_swap_detected is False
    assert neg.risk_signal == "no_recent_change"


async def test_failed_check_never_becomes_negative():
    class Boom(SimSwapProvider):
        name = "boom"

        async def check(self, *, phone_number, max_age_hours):
            raise ProviderUnavailable("operator down")

        async def retrieve_date(self, *, phone_number):
            raise ProviderUnavailable("operator down")

    svc = SimSwapService(Boom(), mock_settings())
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.status == "unknown"
    assert res.sim_swap_detected is None
    assert res.risk_signal == "check_unavailable"


async def test_unsupported_capability_maps_to_unsupported():
    class NoCheck(SimSwapProvider):
        name = "nocheck"
        supports_check = False

        async def check(self, *, phone_number, max_age_hours):
            raise AssertionError("must not be called")

        async def retrieve_date(self, *, phone_number):
            raise UnsupportedCapability("no retrieve-date")

    svc = SimSwapService(NoCheck(), mock_settings())
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.status == "unsupported"
    assert res.risk_signal == "unsupported_operation"
    res2 = await svc.retrieve_date(phone_number="+346661113334")
    assert res2.status == "unsupported"


# -- live provider mapping (mocked HTTP) ---------------------------------

async def _provider_with(handler, **kw) -> GsmaSandboxProvider:
    settings = live_settings(**kw)
    transport = mock_transport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://sandbox.example")
    return GsmaSandboxProvider(settings, client=client)


async def test_live_check_positive_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.url.path == "/sim-swap/v2/check"
        body = json.loads(request.content.decode())
        assert body["phoneNumber"] == "+346661113334"
        assert body["maxAge"] == 72
        return httpx.Response(200, json={"swapped": True})

    provider = await _provider_with(handler)
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.status == "completed" and res.sim_swap_detected is True
    assert res.risk_signal == "recent_sim_change"


async def test_live_check_negative_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"swapped": False})

    provider = await _provider_with(handler)
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.sim_swap_detected is False
    assert res.risk_signal == "no_recent_change"


async def test_live_retrieve_date_with_timestamp():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            200, json={"latestSimChange": "2026-10-08T10:00:00Z"}
        )

    provider = await _provider_with(handler)
    svc = SimSwapService(provider, provider.settings)
    res = await svc.retrieve_date(phone_number="+346661113334")
    assert res.status == "completed"
    assert isinstance(res.last_swap_at, datetime)


async def test_live_retrieve_date_null_with_monitored_period():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            200, json={"latestSimChange": None, "monitoredPeriod": 120}
        )

    provider = await _provider_with(handler)
    svc = SimSwapService(provider, provider.settings)
    res = await svc.retrieve_date(phone_number="+346661113334")
    assert res.status == "completed"
    assert res.last_swap_at is None
    assert res.monitored_period_days == 120
    # Null with monitored period => no visible swap in window, still completed.
    assert res.sim_swap_detected is False


@pytest.mark.parametrize(
    "status_code,exc",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderUnavailable),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (502, ProviderUnavailable),
    ],
)
async def test_http_error_statuses_map_to_unknown(status_code, exc):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(status_code, json={"status": status_code, "code": "X", "message": "err"})

    provider = await _provider_with(handler)
    with pytest.raises(exc):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_malformed_json_maps_to_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, content=b"not-json{{{")

    provider = await _provider_with(handler)
    with pytest.raises(ProviderBadResponse):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_missing_swapped_field_maps_to_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"unexpected": 1})

    provider = await _provider_with(handler)
    with pytest.raises(ProviderBadResponse):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_timeout_maps_to_provider_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow operator")

    provider = await _provider_with(handler)
    with pytest.raises(ProviderTimeout):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_missing_credentials_raise_not_configured():
    settings = mock_settings(auth_flow="client_credentials")  # no URLs/ids
    provider = GsmaSandboxProvider(settings)
    with pytest.raises(ProviderNotConfigured):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_auth_failure_on_token_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    provider = await _provider_with(handler)
    with pytest.raises(ProviderAuthError):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_ciba_omits_phone_number_from_api_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bc-authorize":
            return httpx.Response(200, json={"auth_req_id": "abc"})
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "ciba-tok"})
        body = json.loads(request.content.decode() or "{}")
        seen.update(body)
        return httpx.Response(200, json={"swapped": False})

    provider = await _provider_with(handler, auth_flow="ciba")
    out = await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert out.swapped is False
    # CAMARA 3-legged: phoneNumber MUST NOT be in body.
    assert "phoneNumber" not in seen
    assert seen.get("maxAge") == 72


async def test_two_legged_requires_phone_number():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={"access_token": "tok"})

    provider = await _provider_with(handler, auth_flow="client_credentials")
    with pytest.raises(ProviderBadResponse):
        await provider.check(phone_number=None, max_age_hours=72)


def test_build_provider_returns_mock_when_unconfigured():
    s = mock_settings()
    p = build_provider(s)
    assert isinstance(p, MockSimSwapProvider)

    live = live_settings()
    p2 = build_provider(live)
    assert isinstance(p2, GsmaSandboxProvider)


def test_out_of_range_is_bad_response_not_negative():
    # Service-level: a 400 OUT_OF_RANGE surfaces as unknown, never False.
    async def _run():
        class Range(SimSwapProvider):
            name = "range"

            async def check(self, *, phone_number, max_age_hours):
                raise ProviderBadResponse("OUT_OF_RANGE: exceeds local retention (30 days)")

            async def retrieve_date(self, *, phone_number):
                raise ProviderBadResponse("x")

        svc = SimSwapService(Range(), mock_settings())
        res = await svc.check(phone_number="+346661113334", lookback_hours=2400)
        assert res.status == "unknown"
        assert res.sim_swap_detected is None

    import asyncio

    asyncio.run(_run())


# -- audit regression tests (CAMARA v2.1.0, r3.2/r3.3) -----------------------

async def test_token_expiry_from_expires_in_is_preserved():
    """Regression: expires_in must not be overwritten by the 300s default."""
    token_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            token_calls["n"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"swapped": False})

    provider = await _provider_with(handler)
    try:
        await provider.check(phone_number="+346661113334", max_age_hours=72)
        await provider.check(phone_number="+346661113334", max_age_hours=72)
    finally:
        await provider.aclose()
    assert token_calls["n"] == 1


async def test_empty_scope_blocks_live_with_not_configured():
    settings = live_settings(sim_swap_scope="")
    assert settings.missing_config and "OG_SIM_SWAP_SCOPE" in settings.missing_config
    assert settings.is_live_configured is False
    provider = GsmaSandboxProvider(settings)
    with pytest.raises(ProviderNotConfigured):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_missing_client_secret_blocks_live():
    settings = live_settings(client_secret="")
    assert "OG_CLIENT_SECRET" in settings.missing_config
    assert settings.is_live_configured is False


async def test_ciba_without_bc_url_is_not_live_configured():
    settings = live_settings(auth_flow="ciba", bc_authorize_url="")
    assert "OG_BC_AUTHORIZE_URL" in settings.missing_config
    assert settings.is_live_configured is False


async def test_token_bound_flow_rejects_phone_number():
    """v2.1.0: phoneNumber MUST NOT be sent with token-bound 3-legged flows."""
    settings = live_settings(auth_flow="auth_code")
    transport = mock_transport(lambda req: httpx.Response(200, json={}))
    provider = GsmaSandboxProvider(
        settings, client=httpx.AsyncClient(transport=transport)
    )
    with pytest.raises(ProviderBadResponse, match="MUST NOT"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)
    with pytest.raises(ProviderBadResponse, match="MUST NOT"):
        await provider.retrieve_date(phone_number="+346661113334")


async def test_ciba_requires_phone_for_login_hint():
    settings = live_settings(auth_flow="ciba")
    transport = mock_transport(lambda req: httpx.Response(200, json={}))
    provider = GsmaSandboxProvider(
        settings, client=httpx.AsyncClient(transport=transport)
    )
    with pytest.raises(ProviderBadResponse, match="login_hint"):
        await provider.check(phone_number=None, max_age_hours=72)


async def test_out_of_range_code_is_surfaced_in_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            400,
            json={
                "status": 400,
                "code": "OUT_OF_RANGE",
                "message": "Check monitor period could not exceed local regulations (30 days)",
            },
        )

    provider = await _provider_with(handler)
    with pytest.raises(ProviderBadResponse, match="OUT_OF_RANGE"):
        await provider.check(phone_number="+346661113334", max_age_hours=2400)
    # And at service level it stays unknown, never False.
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=2400)
    assert res.status == "unknown"
    assert res.sim_swap_detected is None
    assert "OUT_OF_RANGE" in (res.detail or "")


async def test_404_identifier_not_found_code_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            404,
            json={
                "status": 404,
                "code": "IDENTIFIER_NOT_FOUND",
                "message": "Identifier not found",
            },
        )

    provider = await _provider_with(handler)
    with pytest.raises(ProviderUnavailable, match="IDENTIFIER_NOT_FOUND"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_x_correlator_header_is_sent_and_recorded():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen["x_correlator"] = request.headers.get("x-correlator")
        return httpx.Response(200, json={"swapped": True})

    provider = await _provider_with(handler)
    out = await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert seen["x_correlator"]
    assert out.x_correlator == seen["x_correlator"]
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.evidence[0].get("x_correlator")


def test_build_provider_requires_scope_secret_and_ciba_url():
    assert isinstance(build_provider(mock_settings()), MockSimSwapProvider)
    assert isinstance(build_provider(live_settings()), GsmaSandboxProvider)
    no_scope = live_settings(sim_swap_scope="")
    assert isinstance(build_provider(no_scope), MockSimSwapProvider)
    no_secret = live_settings(client_secret="")
    assert isinstance(build_provider(no_secret), MockSimSwapProvider)
    ciba_no_bc = live_settings(auth_flow="ciba", bc_authorize_url="")
    assert isinstance(build_provider(ciba_no_bc), MockSimSwapProvider)


# -- v1.0.0 compatibility (r1.4; default behavior unchanged) ------------------

def test_version_defaults_to_v2():
    assert live_settings().sim_swap_api_version == "v2"
    assert live_settings().is_v1 is False
    assert live_settings(sim_swap_api_version="v1").is_v1 is True


async def test_v1_token_bound_flow_passes_phone_through_for_server_validation():
    """v1 lenient rule: phone MAY accompany 3-legged tokens (match checked server-side)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bc-authorize":
            return httpx.Response(200, json={"auth_req_id": "abc"})
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen.update(json.loads(request.content.decode() or "{}"))
        return httpx.Response(200, json={"swapped": False})

    settings = live_settings(auth_flow="ciba", sim_swap_api_version="v1")
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    out = await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert out.swapped is False
    assert seen.get("phoneNumber") == "+346661113334"


async def test_v1_auth_code_guard_does_not_reject_phone():
    """v1 lets the server validate phone/token match; guard must not 422 first."""
    settings = live_settings(auth_flow="auth_code", sim_swap_api_version="v1")
    provider = SimSwapV1Provider(
        settings,
        client=httpx.AsyncClient(
            transport=mock_transport(lambda req: httpx.Response(200, json={}))
        ),
    )
    # Passes the phone guard, then stops at the (MVP-unimplemented) external
    # token step — never a phone-validation error.
    with pytest.raises(ProviderNotConfigured, match="externally acquired"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_v1_invalid_token_context_is_surfaced_as_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            403,
            json={
                "status": 403,
                "code": "INVALID_TOKEN_CONTEXT",
                "message": "phoneNumber is not consistent with access token",
            },
        )

    settings = live_settings(sim_swap_api_version="v1")
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    with pytest.raises(ProviderAuthError, match="INVALID_TOKEN_CONTEXT"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_v1_unidentifiable_phone_number_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            422,
            json={
                "status": 422,
                "code": "UNIDENTIFIABLE_PHONE_NUMBER",
                "message": "The phone number cannot be identified",
            },
        )

    settings = live_settings(sim_swap_api_version="v1")
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    with pytest.raises(ProviderBadResponse, match="UNIDENTIFIABLE_PHONE_NUMBER"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_v1_retrieve_date_without_monitored_period():
    """v1 SimSwapInfo has no monitoredPeriod property; absent must map to None."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"latestSimChange": None})

    settings = live_settings(sim_swap_api_version="v1")
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    out = await provider.retrieve_date(phone_number="+346661113334")
    assert out.latest_sim_change is None
    assert out.monitored_period_days is None


# -- portal v1 vocabulary + separate-adapter guarantees ------------------------

def test_factory_selects_separate_v1_adapter():
    v1 = build_provider(live_settings(sim_swap_api_version="v1"))
    v2 = build_provider(live_settings(sim_swap_api_version="v2"))
    assert isinstance(v1, SimSwapV1Provider)
    assert isinstance(v2, GsmaSandboxProvider)
    assert not isinstance(v2, SimSwapV1Provider)
    assert v1.api_version == "v1" and v2.api_version == "v2"
    # Portal v1 error vocabulary is declared on the v1 class.
    for code in ("INVALID_ARGUMENT", "NOT_SUPPORTED", "INTERNAL", "UNAVAILABLE", "TIMEOUT"):
        assert code in SimSwapV1Provider.documented_errors
    # v2 keeps its own distinct vocabulary.
    assert "UNNECESSARY_IDENTIFIER" in GsmaSandboxProvider.documented_errors
    assert "UNNECESSARY_IDENTIFIER" not in SimSwapV1Provider.documented_errors


async def test_v1_portal_422_not_supported_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            422,
            json={
                "status": 422,
                "code": "NOT_SUPPORTED",
                "message": "Service not supported for this phoneNumber",
            },
        )

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    with pytest.raises(ProviderBadResponse, match="NOT_SUPPORTED"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)
    # Service level: stays unknown, never False.
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.status == "unknown" and res.sim_swap_detected is None


@pytest.mark.parametrize(
    "http_status,code",
    [(500, "INTERNAL"), (503, "UNAVAILABLE"), (504, "TIMEOUT")],
)
async def test_v1_5xx_codes_preserved_as_unavailable(http_status, code):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            http_status,
            json={"status": http_status, "code": code, "message": "portal example"},
        )

    for cls, ver in ((SimSwapV1Provider, "v1"), (GsmaSandboxProvider, "v2")):
        provider = cls(
            live_settings(sim_swap_api_version=ver),
            client=httpx.AsyncClient(transport=mock_transport(handler)),
        )
        with pytest.raises(ProviderUnavailable, match=code):
            await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_v2_still_rejects_token_bound_phone():
    settings = live_settings(auth_flow="auth_code", sim_swap_api_version="v2")
    provider = GsmaSandboxProvider(
        settings,
        client=httpx.AsyncClient(
            transport=mock_transport(lambda req: httpx.Response(200, json={}))
        ),
    )
    with pytest.raises(ProviderBadResponse, match="MUST NOT"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


async def test_retrieve_date_sends_unique_correlator():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen.append(request.headers.get("x-correlator"))
        return httpx.Response(200, json={"latestSimChange": None})

    provider = await _provider_with(handler)
    await provider.retrieve_date(phone_number="+346661113334")
    await provider.retrieve_date(phone_number="+346661113334")
    assert len(seen) == 2 and seen[0] and seen[1] and seen[0] != seen[1]


# -- supplied v1 portal examples as source of truth ----------------------------

async def test_v1_portal_check_example_exact_shape():
    """Portal: POST /check {"phoneNumber": "+346661113334", "maxAge": 240} -> {"swapped": true}."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen.update(json.loads(request.content.decode() or "{}"))
        return httpx.Response(200, json={"swapped": True})

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    out = await provider.check(phone_number="+346661113334", max_age_hours=240)
    assert out.swapped is True
    assert seen == {"phoneNumber": "+346661113334", "maxAge": 240}


async def test_v1_portal_retrieve_example_timezone_offset():
    """Portal: -> {"latestSimChange": "2023-07-03T14:27:08.312+02:00"} keeps +02:00."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            200, json={"latestSimChange": "2023-07-03T14:27:08.312+02:00"}
        )

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    out = await provider.retrieve_date(phone_number="+346661113334")
    assert out.latest_sim_change is not None
    assert out.latest_sim_change.utcoffset() is not None
    assert out.latest_sim_change.utcoffset().total_seconds() == 2 * 3600
    assert out.monitored_period_days is None  # v1 has no monitoredPeriod


@pytest.mark.parametrize("bad_age", [0, -5, 2401, 9999, "72", 72.5])
async def test_maxAge_outside_1_2400_rejected_at_adapter(bad_age):
    for cls, ver in ((SimSwapV1Provider, "v1"), (GsmaSandboxProvider, "v2")):
        provider = cls(
            live_settings(sim_swap_api_version=ver),
            client=httpx.AsyncClient(
                transport=mock_transport(lambda req: httpx.Response(200, json={}))
            ),
        )
        with pytest.raises(ProviderBadResponse, match="1..2400"):
            await provider.check(phone_number="+346661113334", max_age_hours=bad_age)


async def test_v1_portal_400_invalid_argument():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            400,
            json={
                "status": 400,
                "code": "INVALID_ARGUMENT",
                "message": "Client specified an invalid argument.",
            },
        )

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    with pytest.raises(ProviderBadResponse, match="INVALID_ARGUMENT"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)
    svc = SimSwapService(provider, provider.settings)
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.status == "unknown" and res.sim_swap_detected is None
    assert "INVALID_ARGUMENT" in (res.detail or "")


@pytest.mark.parametrize(
    "http_status,code,exc",
    [
        (401, "UNAUTHENTICATED", ProviderAuthError),
        (403, "PERMISSION_DENIED", ProviderAuthError),
        (404, "NOT_FOUND", ProviderUnavailable),
    ],
)
async def test_v1_portal_auth_notfound_codes_preserved(http_status, code, exc):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(
            http_status,
            json={"status": http_status, "code": code, "message": "portal example"},
        )

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    with pytest.raises(exc, match=code):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


@pytest.mark.parametrize("bad_ts", [12345, True, "not-a-date", "2023-13-99T99:99:99Z"])
async def test_malformed_latestSimChange_rejected(bad_ts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"latestSimChange": bad_ts})

    for cls, ver in ((SimSwapV1Provider, "v1"), (GsmaSandboxProvider, "v2")):
        provider = cls(
            live_settings(sim_swap_api_version=ver),
            client=httpx.AsyncClient(transport=mock_transport(handler)),
        )
        with pytest.raises(ProviderBadResponse):
            await provider.retrieve_date(phone_number="+346661113334")


# -- confirmed sandbox v1 configuration (offline; no live calls) ---------------
# Portal-confirmed 2026-10-09: server https://open-gateway-sandbox.gsma.com,
# SIM Swap v1.0.0, Client Credentials, scopes sim-swap:check +
# sim-swap:retrieve-date, POST /sim-swap/v1/check + /sim-swap/v1/retrieve-date.
# Token URL + client-auth method are NOT confirmed (no portal token example);
# tests below use a mock transport only and invent nothing routable.

CONFIRMED_V1_BASE = "https://open-gateway-sandbox.gsma.com/sim-swap/v1"
CONFIRMED_V1_SCOPES = "sim-swap:check sim-swap:retrieve-date"


def confirmed_v1_settings(**overrides) -> Settings:
    kw = {
        "auth_flow": "client_credentials",
        "api_base_url": CONFIRMED_V1_BASE,
        "sim_swap_api_version": "v1",
        "sim_swap_scope": CONFIRMED_V1_SCOPES,
    }
    kw.update(overrides)
    return live_settings(**kw)


def test_confirmed_v1_settings_select_v1_adapter():
    provider = build_provider(confirmed_v1_settings())
    assert isinstance(provider, SimSwapV1Provider)
    assert provider.settings.api_base_url == CONFIRMED_V1_BASE
    assert provider.settings.sim_swap_scope == CONFIRMED_V1_SCOPES
    # v2 default path is untouched.
    assert isinstance(build_provider(live_settings()), GsmaSandboxProvider)


async def test_confirmed_v1_final_urls_match_portal_exactly():
    """Base + path must equal the confirmed endpoints with no duplication."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen_urls.append(str(request.url))
        if request.url.path.endswith("/check"):
            return httpx.Response(200, json={"swapped": False})
        return httpx.Response(200, json={"latestSimChange": None})

    provider = SimSwapV1Provider(
        confirmed_v1_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    await provider.check(phone_number="+346661113334", max_age_hours=72)
    await provider.retrieve_date(phone_number="+346661113334")
    assert seen_urls == [
        f"{CONFIRMED_V1_BASE}/check",
        f"{CONFIRMED_V1_BASE}/retrieve-date",
    ]
    for url in seen_urls:
        assert url.count("/sim-swap/v1") == 1
        assert "/sim-swap/v1/sim-swap" not in url
        assert "//sim-swap" not in url.split("://", 1)[1]


async def test_confirmed_v1_trailing_slash_base_has_no_double_slash():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert "//sim-swap" not in str(request.url).split("://", 1)[1]
        return httpx.Response(200, json={"swapped": False})

    settings = confirmed_v1_settings(api_base_url=CONFIRMED_V1_BASE + "/")
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    out = await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert out.swapped is False


async def test_confirmed_v1_token_request_shape():
    """Client-credentials token POST: grant_type + verbatim scope; no invented URL."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen["url"] = str(request.url)
            seen["auth_present"] = bool(request.headers.get("Authorization", ""))
            body = request.content.decode()
            seen["grant"] = "grant_type=client_credentials" in body
            # Scope sent verbatim (URL-encoded); both confirmed scopes present.
            seen["scope_ok"] = "sim-swap%3Acheck" in body and "sim-swap%3Aretrieve-date" in body
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"swapped": False})

    provider = SimSwapV1Provider(
        confirmed_v1_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert seen["grant"] is True
    assert seen["scope_ok"] is True
    assert seen["auth_present"] is True
    # Token values themselves are never asserted/logged beyond presence.


async def test_confirmed_v1_request_bodies_match_portal():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/check"):
            seen["check"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"swapped": True})
        seen["retrieve"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"latestSimChange": None})

    provider = SimSwapV1Provider(
        confirmed_v1_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    await provider.check(phone_number="+346661113334", max_age_hours=240)
    await provider.retrieve_date(phone_number="+346661113334")
    assert seen["check"] == {"phoneNumber": "+346661113334", "maxAge": 240}
    assert seen["retrieve"] == {"phoneNumber": "+346661113334"}


async def test_confirmed_v1_missing_token_url_stays_mock():
    """Token URL unverified -> factory must stay mock; nothing invented."""
    settings = confirmed_v1_settings(token_url="")
    assert "OG_TOKEN_URL" in settings.missing_config
    assert settings.is_live_configured is False
    assert isinstance(build_provider(settings), MockSimSwapProvider)


# -- risk-semantics regression tests (audit) -----------------------------------

async def test_retrieve_null_without_monitored_period_is_unknown():
    """v1-style null (no monitoredPeriod, retention unknowable) must never be
    a negative result — unknown with detected=None, explicitly not safe."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"latestSimChange": None})

    for cls, ver in ((SimSwapV1Provider, "v1"), (GsmaSandboxProvider, "v2")):
        provider = cls(
            live_settings(sim_swap_api_version=ver),
            client=httpx.AsyncClient(transport=mock_transport(handler)),
        )
        svc = SimSwapService(provider, provider.settings)
        res = await svc.retrieve_date(phone_number="+346661113334")
        assert res.status == "unknown"
        assert res.sim_swap_detected is None
        assert res.risk_signal == "check_unavailable"
        assert "not evidence of safety" in (res.detail or "")
        assert res.evidence[0]["latest_sim_change"] is None


async def test_check_completed_details_are_calibrated():
    def handler(swapped: bool):
        def _h(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"swapped": swapped})

        return _h

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler(True))),
    )
    svc = SimSwapService(provider, provider.settings)
    pos = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert pos.status == "completed" and pos.sim_swap_detected is True
    assert "not proof of fraud" in (pos.detail or "")

    provider2 = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler(False))),
    )
    neg = await SimSwapService(provider2, provider2.settings).check(
        phone_number="+346661113334", lookback_hours=72
    )
    assert neg.status == "completed" and neg.sim_swap_detected is False
    assert "does not mean" in (neg.detail or "") and "safe" in (neg.detail or "")


async def test_bearer_token_is_reread_not_cached():
    """A rotated bearer token must take effect without restarting the process."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"swapped": False})

    settings = bearer_settings()
    provider = SimSwapV1Provider(
        settings, client=httpx.AsyncClient(transport=mock_transport(handler))
    )
    await provider.check(phone_number="+346661113334", max_age_hours=72)
    settings.bearer_token = "rotated-token-NOT-A-SECRET"
    await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert seen == [f"Bearer {FAKE_BEARER}", "Bearer rotated-token-NOT-A-SECRET"]


# -- http_status plumbing (audit: summaries must show the real HTTP status) ----

async def test_completed_evidence_carries_http_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path.endswith("/check"):
            return httpx.Response(200, json={"swapped": False})
        return httpx.Response(
            200, json={"latestSimChange": "2023-07-03T14:27:08.312+02:00"}
        )

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    svc = SimSwapService(provider, provider.settings)
    check_res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert check_res.evidence[0]["http_status"] == 200
    date_res = await svc.retrieve_date(phone_number="+346661113334")
    assert date_res.evidence[0]["http_status"] == 200


async def test_unknown_null_keeps_http_status_for_diagnosis():
    """HTTP 200 with null payload is still unknown — and the summary must show
    http_status=200 (transport OK) alongside status=unknown (no verdict)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"latestSimChange": None})

    provider = SimSwapV1Provider(
        live_settings(sim_swap_api_version="v1"),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    res = await SimSwapService(provider, provider.settings).retrieve_date(
        phone_number="+346661113334"
    )
    assert res.status == "unknown"
    assert res.evidence[0]["http_status"] == 200


def test_live_summary_format_is_sanitized():
    """Offline check of the live-test summary: HTTP status present, and no
    full phone number or token material can appear in it."""
    from app.schemas import SimSwapResult
    from tests.test_live_sim_swap import _summarize

    fake_token_fragment = "test-bearer-NOT-A-SECRET"
    res = SimSwapResult(
        status="unknown",
        provider="gsma-sandbox-v1",
        lookback_hours=72,
        risk_signal="check_unavailable",
        masked_phone="+44******3308",
        detail="ProviderAuthError: 401 UNAUTHENTICATED",
        evidence=[{"type": "sim_swap_check", "http_status": 401}],
    )
    line = _summarize("check", res)
    assert "http_status=401" in line
    assert "status=unknown" in line
    assert "+346661113334" not in line
    assert fake_token_fragment not in line
    assert "Authorization" not in line


# -- bearer-token mode (portal-issued token, env-only, no token endpoint) -----
# Fake token below is NOT a secret; it only exercises the offline code path.

FAKE_BEARER = "test-bearer-token-NOT-A-SECRET"


def bearer_settings(**overrides) -> Settings:
    kw = {
        "auth_flow": "bearer",
        "api_base_url": CONFIRMED_V1_BASE,
        "sim_swap_api_version": "v1",
        "bearer_token": FAKE_BEARER,
    }
    kw.update(overrides)
    base = Settings(
        provider="gsma-sandbox",
        client_id="",
        client_secret="",
        api_base_url="",
        token_url="",
        bc_authorize_url="",
        sim_swap_scope="",
        auth_flow="mock",
        timeout_seconds=5.0,
        default_lookback_hours=72,
        max_lookback_hours=2400,
        sim_swap_api_version="v2",
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def test_bearer_requires_only_token_and_base_url():
    s = bearer_settings()
    assert s.is_live_configured is True
    assert s.missing_config == []
    assert isinstance(build_provider(s), SimSwapV1Provider)
    # v2 + bearer stays strict v2.
    v2 = build_provider(bearer_settings(sim_swap_api_version="v2"))
    assert isinstance(v2, GsmaSandboxProvider)
    assert not isinstance(v2, SimSwapV1Provider)
    # Missing token -> mock, never live.
    assert isinstance(build_provider(bearer_settings(bearer_token="")), MockSimSwapProvider)
    assert "OG_SIM_SWAP_BEARER_TOKEN" in bearer_settings(bearer_token="").missing_config


async def test_bearer_v1_makes_no_token_endpoint_call():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path not in ("/token", "/bc-authorize"), (
            "bearer mode must never call a token endpoint"
        )
        auth = request.headers.get("Authorization", "")
        assert auth == f"Bearer {FAKE_BEARER}"
        return httpx.Response(200, json={"swapped": False})

    provider = SimSwapV1Provider(
        bearer_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    out = await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert out.swapped is False
    assert calls and all(c in ("/sim-swap/v1/check",) for c in calls)


async def test_bearer_v1_retrieve_date_sends_bearer_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers.get("Authorization", "") == f"Bearer {FAKE_BEARER}"
        return httpx.Response(200, json={"latestSimChange": None})

    provider = SimSwapV1Provider(
        bearer_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    out = await provider.retrieve_date(phone_number="+346661113334")
    assert out.latest_sim_change is None
    assert calls == ["/sim-swap/v1/retrieve-date"]


async def test_bearer_missing_token_fails_before_http():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, json={"swapped": False})

    provider = SimSwapV1Provider(
        bearer_settings(bearer_token=""),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    with pytest.raises(ProviderNotConfigured, match="OG_SIM_SWAP_BEARER_TOKEN"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)
    assert calls == []


async def test_bearer_invalid_inputs_fail_before_http():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, json={"swapped": False})

    from app.schemas import CheckRequest as _CR

    with pytest.raises(Exception):
        _CR(phone_number="bad-number", lookback_hours=72)
    provider = SimSwapV1Provider(
        bearer_settings(),
        client=httpx.AsyncClient(transport=mock_transport(handler)),
    )
    with pytest.raises(ProviderBadResponse, match="1..2400"):
        await provider.check(phone_number="+346661113334", max_age_hours=0)
    assert calls == []


async def test_bearer_v2_still_rejects_token_bound_phone():
    provider = GsmaSandboxProvider(
        bearer_settings(sim_swap_api_version="v2"),
        client=httpx.AsyncClient(
            transport=mock_transport(lambda req: httpx.Response(200, json={}))
        ),
    )
    with pytest.raises(ProviderBadResponse, match="MUST NOT"):
        await provider.check(phone_number="+346661113334", max_age_hours=72)


def test_settings_repr_never_leaks_secrets():
    s = bearer_settings()
    s.client_secret = "super-secret-value"
    dumped = repr(s)
    assert FAKE_BEARER not in dumped
    assert "super-secret-value" not in dumped
