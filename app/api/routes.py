"""HTTP routes: /api/security/sim-swap/check and /retrieve-date."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.schemas import CheckRequest, RetrieveDateRequest, SimSwapResult
from app.services.providers.gsma_sandbox import build_provider
from app.services.sim_swap import SimSwapService

router = APIRouter(prefix="/api/security/sim-swap", tags=["sim-swap"])


async def get_service(
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[SimSwapService]:
    # Generator dependency so the provider's HTTP client is always closed
    # after the request (live mode opens a real httpx pool per request).
    provider = build_provider(settings)
    try:
        yield SimSwapService(provider=provider, settings=settings)
    finally:
        await provider.aclose()


@router.post("/check", response_model=SimSwapResult, summary="Check recent SIM swap")
async def check_sim_swap(
    body: CheckRequest, service: SimSwapService = Depends(get_service)
) -> SimSwapResult:
    """CAMARA POST /check. lookback_hours maps to CAMARA maxAge (1..2400)."""
    return await service.check(
        phone_number=body.phone_number, lookback_hours=body.lookback_hours
    )


@router.post(
    "/retrieve-date", response_model=SimSwapResult, summary="Retrieve last swap date"
)
async def retrieve_sim_swap_date(
    body: RetrieveDateRequest, service: SimSwapService = Depends(get_service)
) -> SimSwapResult:
    """CAMARA POST /retrieve-date. Returns latestSimChange when supported."""
    return await service.retrieve_date(phone_number=body.phone_number)
