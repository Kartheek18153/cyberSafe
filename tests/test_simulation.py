"""Simulation tests: stateful temp registry + management endpoints (all offline)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app
from app.schemas import SimulatorSwapRequest
from app.services.providers.base import ProviderUnavailable
from app.services.providers.gsma_sandbox import GsmaSandboxProvider, build_provider
from app.services.providers.simulation import STORE, SimulatedSimSwapProvider
from app.services.sim_swap import SimSwapService


@pytest.fixture(autouse=True)
def _fresh_store():
    STORE.reset(seed=True)
    yield
    STORE.reset(seed=True)


def _sim_settings() -> Settings:
    return Settings(
        provider="simulation",
        client_id="",
        client_secret="",
        api_base_url="",
        token_url="",
        bc_authorize_url="",
        sim_swap_scope="",
        auth_flow="simulation",
        timeout_seconds=5.0,
        default_lookback_hours=72,
        max_lookback_hours=2400,
    )


def test_factory_selects_simulation():
    provider = build_provider(_sim_settings())
    assert isinstance(provider, SimulatedSimSwapProvider)
    assert not isinstance(provider, GsmaSandboxProvider)


async def test_seeded_demo_numbers_behave():
    svc = SimSwapService(SimulatedSimSwapProvider(), _sim_settings())
    clean = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert clean.status == "completed" and clean.sim_swap_detected is False
    swapped = await svc.check(phone_number="+3412340000", lookback_hours=72)
    assert swapped.status == "completed" and swapped.sim_swap_detected is True
    assert swapped.risk_signal == "recent_sim_change"


async def test_recorded_swap_flips_check_and_retrieve():
    svc = SimSwapService(SimulatedSimSwapProvider(), _sim_settings())
    before = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert before.sim_swap_detected is False
    STORE.record_swap("+346661113334")
    after = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert after.status == "completed" and after.sim_swap_detected is True
    date = await svc.retrieve_date(phone_number="+346661113334")
    assert date.status == "completed"
    assert date.last_swap_at is not None
    assert date.risk_signal == "recent_sim_change"


async def test_old_swap_outside_window_is_negative():
    from datetime import datetime, timedelta, timezone

    STORE.record_swap(
        "+346661113334",
        datetime.now(timezone.utc) - timedelta(hours=200),
    )
    svc = SimSwapService(SimulatedSimSwapProvider(), _sim_settings())
    res = await svc.check(phone_number="+346661113334", lookback_hours=72)
    assert res.sim_swap_detected is False
    res2 = await svc.check(phone_number="+346661113334", lookback_hours=240)
    assert res2.sim_swap_detected is True


async def test_retrieve_without_swaps_returns_activation_date():
    svc = SimSwapService(SimulatedSimSwapProvider(), _sim_settings())
    res = await svc.retrieve_date(phone_number="+346661113334")
    assert res.status == "completed"
    assert res.last_swap_at is not None
    assert res.sim_swap_detected is False  # activation long predates the window


async def test_unknown_subscriber_is_unknown_never_negative():
    svc = SimSwapService(SimulatedSimSwapProvider(), _sim_settings())
    res = await svc.check(phone_number="+12025550100", lookback_hours=72)
    assert res.status == "unknown"
    assert res.sim_swap_detected is None
    assert res.risk_signal == "check_unavailable"


async def test_provider_raises_unavailable_for_unknown():
    provider = SimulatedSimSwapProvider()
    with pytest.raises(ProviderUnavailable):
        await provider.check(phone_number="+12025550100", max_age_hours=72)


def test_simulator_swap_request_validation():
    assert SimulatorSwapRequest(phone_number="+346661113334").at is None
    with pytest.raises(ValidationError):
        SimulatorSwapRequest(phone_number="bad-number")


def test_full_interactive_flow_through_http():
    """End-to-end temp-data flow over HTTP: add -> clean -> swap -> flagged."""
    from app.config import get_settings

    settings = _sim_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            assert client.get("/health").json()["auth_flow"] == "simulation"
            added = client.post(
                "/api/simulator/subscribers", json={"phone_number": "+12025550100"}
            )
            assert added.status_code == 200
            assert added.json()["swap_count"] == 0
            assert "+12025550100" not in added.text

            clean = client.post(
                "/api/security/sim-swap/check",
                json={"phone_number": "+12025550100", "lookback_hours": 72},
            ).json()
            assert clean["sim_swap_detected"] is False

            swap = client.post(
                "/api/simulator/swaps", json={"phone_number": "+12025550100"}
            )
            assert swap.status_code == 200

            flagged = client.post(
                "/api/security/sim-swap/check",
                json={"phone_number": "+12025550100", "lookback_hours": 72},
            ).json()
            assert flagged["sim_swap_detected"] is True
            assert flagged["risk_signal"] == "recent_sim_change"

            listing = client.get("/api/simulator/subscribers").json()
            assert listing["temporary"] is True
            assert "+12025550100" not in str(listing)

            reset = client.post("/api/simulator/reset")
            assert reset.status_code == 200
            assert reset.json()["reset"] is True
    finally:
        app.dependency_overrides.clear()


def test_reset_restores_demo_defaults():
    STORE.record_swap("+346661113334")
    STORE.reset(seed=True)
    record = STORE.get("+346661113334")
    assert record is not None and record.swaps == []
