"""API tests using FastAPI TestClient. No live network."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.services.providers.base import SimSwapProvider, UnsupportedCapability


def _override(settings: Settings):
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


def _mock_settings() -> Settings:
    return Settings(
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
    )


def setup_function(_):
    app.dependency_overrides.clear()


def teardown_function(_):
    app.dependency_overrides.clear()


def test_health_reports_mock_mode():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["live_configured"] is False
    assert body["auth_flow"] == "mock"


def test_check_positive_and_masks_phone():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": "+3412340000", "lookback_hours": 72},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["sim_swap_detected"] is True
    assert body["risk_signal"] == "recent_sim_change"
    assert "+3412340000" not in resp.text
    assert body["masked_phone"].endswith("0000")


def test_check_negative():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": "+346661113334", "lookback_hours": 72},
        )
    assert resp.status_code == 200
    assert resp.json()["sim_swap_detected"] is False
    assert resp.json()["risk_signal"] == "no_recent_change"


def test_check_rejects_bad_phone_with_422():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": "bad", "lookback_hours": 72},
        )
    assert resp.status_code == 422


def test_check_rejects_bad_window_with_422():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": "+346661113334", "lookback_hours": 9999},
        )
    assert resp.status_code == 422


def test_retrieve_date_ok():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/retrieve-date",
            json={"phone_number": "+346661113334"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"


def test_unsupported_operation_returns_unsupported_not_error():
    from app.api.routes import get_service
    from app.services.sim_swap import SimSwapService

    class NoRetrieve(SimSwapProvider):
        name = "noretrieve"
        supports_retrieve_date = False

        async def check(self, *, phone_number, max_age_hours):
            from app.services.providers.base import CheckOutcome

            return CheckOutcome(swapped=False, max_age_hours=max_age_hours)

        async def retrieve_date(self, *, phone_number):
            raise UnsupportedCapability("disabled")

    settings = _mock_settings()
    app.dependency_overrides[get_service] = lambda: SimSwapService(
        NoRetrieve(), settings
    )
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/security/sim-swap/retrieve-date",
                json={"phone_number": "+346661113334"},
            )
    finally:
        app.dependency_overrides.pop(get_service, None)
    assert resp.status_code == 200
    assert resp.json()["status"] == "unsupported"
    assert resp.json()["risk_signal"] == "unsupported_operation"


def test_api_request_closes_provider_client():
    """Audit: every HTTP request must close the provider client (no pool leak)."""
    import app.api.routes as routes_mod
    from app.services.providers.gsma_sandbox import MockSimSwapProvider

    closed = {"v": False}
    provider = MockSimSwapProvider()
    real_aclose = provider.aclose

    async def tracking_aclose():
        closed["v"] = True
        await real_aclose()

    provider.aclose = tracking_aclose  # type: ignore[method-assign]
    original = routes_mod.build_provider
    routes_mod.build_provider = lambda settings, client=None: provider  # type: ignore[assignment]
    _override(_mock_settings())
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/security/sim-swap/check",
                json={"phone_number": "+346661113334", "lookback_hours": 72},
            )
    finally:
        routes_mod.build_provider = original
    assert resp.status_code == 200
    assert closed["v"] is True


def test_api_check_detail_disclaims_safety():
    _override(_mock_settings())
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": "+346661113334", "lookback_hours": 72},
        )
    assert resp.status_code == 200
    assert "safe" in (resp.json()["detail"] or "")
