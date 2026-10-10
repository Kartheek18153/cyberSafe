"""FastAPI entrypoint: app.main:app."""

from __future__ import annotations

from fastapi import Depends, FastAPI

from app.api.routes import router as sim_swap_router
from app.api.simulator import router as simulator_router
from app.config import Settings, get_settings
from app.schemas import HealthResponse

app = FastAPI(
    title="Cybersafe SIM Swap API",
    description=(
        "CAMARA-compliant SIM Swap detection for the AI-Powered Mobile "
        "Cybercrime Prevention engine. A recent SIM swap is a risk signal, "
        "never proof of fraud; absence of a swap never means 'safe'."
    ),
    version="0.1.0",
)

app.include_router(sim_swap_router)
app.include_router(simulator_router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        provider=settings.provider,
        auth_flow=settings.auth_flow,
        live_configured=settings.is_live_configured,
        missing_config=settings.missing_config,
    )


@app.get("/", tags=["health"])
def root() -> dict:
    return {"service": "cybersafe-sim-swap", "docs": "/docs", "health": "/health"}
