"""Opt-in live sandbox test for SimSwapV1Provider with a portal-issued bearer token.

Opt-in ONLY — skipped unless BOTH conditions hold:
    OG_LIVE_TEST=1  and  OG_SIM_SWAP_BEARER_TOKEN is set in the local environment.

Pass criteria are STRICT: the test FAILS unless BOTH operations return
``status="completed"``. An ``unknown`` or ``unsupported`` result can never
pass silently — a pytest pass therefore proves both GSMA calls succeeded
with HTTP 200 and valid bodies.

The bearer token is read from the environment at call time and is NEVER
printed, logged, committed, or asserted on. Per-operation summaries contain
only sanitized fields (masked phone, status codes, provider error
codes/messages — never headers or token contents).

Run (PowerShell, from the project directory) after filling local .env /
environment and explicit approval:
    $env:OG_LIVE_TEST="1"
    pytest tests/test_live_sim_swap.py -q

Never part of the standard suite. Makes REAL network calls when enabled.

Note: Swagger example numbers are parsing fixtures, not verified live test
cases. A completed result for one proves connectivity and response parsing —
not the subscriber's real SIM history nor real-world detection accuracy.
"""

import os

import pytest

from app.config import get_settings
from app.schemas import CheckRequest, RetrieveDateRequest, SimSwapResult
from app.services.providers.base import ProviderError
from app.services.providers.gsma_sandbox import SimSwapV1Provider, build_provider
from app.services.sim_swap import SimSwapService

pytestmark = pytest.mark.skipif(
    os.getenv("OG_LIVE_TEST") != "1" or not os.getenv("OG_SIM_SWAP_BEARER_TOKEN"),
    reason=(
        "Live test requires OG_LIVE_TEST=1 AND OG_SIM_SWAP_BEARER_TOKEN set locally. "
        "Skipped by default; mock mode is unaffected."
    ),
)


def _http_status_of(result: SimSwapResult) -> object:
    """Actual HTTP status of the provider call, if a response was received.
    Timeouts / connection failures yield None (no HTTP response exists)."""
    for entry in result.evidence:
        if isinstance(entry, dict) and "http_status" in entry:
            return entry["http_status"]
    return None


def _summarize(operation: str, result: SimSwapResult) -> str:
    """One-line sanitized summary. Only safe fields: HTTP status, result
    status, risk signal, boolean outcome, masked phone, provider code/message
    detail. Never the token, authorization headers, or the full phone number."""
    return (
        f"operation={operation} http_status={_http_status_of(result)} "
        f"status={result.status} risk_signal={result.risk_signal} "
        f"sim_swap_detected={result.sim_swap_detected} "
        f"masked_phone={result.masked_phone} detail={result.detail}"
    )


async def test_live_v1_check_and_retrieve_date_with_bearer():
    settings = get_settings()
    assert settings.auth_flow == "bearer", (
        f"Live bearer test needs OG_AUTH_FLOW=bearer (got {settings.auth_flow!r})."
    )
    assert settings.is_v1, (
        f"Live bearer test needs OG_SIM_SWAP_API_VERSION=v1 (got {settings.sim_swap_api_version!r})."
    )
    assert settings.is_live_configured, (
        "Live bearer test needs OG_SIM_SWAP_BEARER_TOKEN + OG_API_BASE_URL. "
        f"Missing: {settings.missing_config}. See README section 12."
    )
    # Inputs validated BEFORE any request (E.164 + maxAge 1..2400).
    phone = os.getenv("OG_TEST_PHONE_NUMBER", "")
    check_req = CheckRequest(phone_number=phone, lookback_hours=72)
    RetrieveDateRequest(phone_number=phone)

    provider = build_provider(settings)
    assert isinstance(provider, SimSwapV1Provider)
    try:
        svc = SimSwapService(provider, settings)
        check_res = await svc.check(
            phone_number=check_req.phone_number,
            lookback_hours=check_req.lookback_hours,
        )
        print(f"[live] {_summarize('check', check_res)}")
        date_res = await svc.retrieve_date(phone_number=check_req.phone_number)
        print(f"[live] {_summarize('retrieve-date', date_res)}")
    except ProviderError as exc:
        # Sanitized: exception type only — messages may carry provider codes
        # but never authorization headers or token contents.
        pytest.fail(f"Live sandbox request failed: {type(exc).__name__}")
    finally:
        await provider.aclose()
    # STRICT: any unknown/unsupported fails. A pass proves both GSMA calls
    # returned HTTP 200 with valid response bodies.
    assert check_res.status == "completed", (
        f"/check did not succeed: {_summarize('check', check_res)}"
    )
    assert isinstance(check_res.sim_swap_detected, bool), (
        f"/check completed without a boolean outcome: {_summarize('check', check_res)}"
    )
    assert date_res.status == "completed", (
        f"/retrieve-date did not succeed: {_summarize('retrieve-date', date_res)}"
    )
