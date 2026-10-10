"""Simulator management endpoints: mutate temporary simulation data.

All state is volatile in-process memory (see providers/simulation.py).
Every response carries masked phones only.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas import RetrieveDateRequest, SimulatorSwapRequest, mask_phone
from app.services.providers.simulation import STORE

router = APIRouter(prefix="/api/simulator", tags=["simulator (temporary data)"])


@router.get("/subscribers", summary="List simulated subscribers (masked)")
def list_subscribers() -> dict:
    return {"subscribers": STORE.snapshot(), "temporary": True}


@router.post("/subscribers", summary="Add a simulated subscriber")
def add_subscriber(body: RetrieveDateRequest) -> dict:
    if body.phone_number is None:
        return {"error": "phone_number is required", "masked_phone": None}
    record = STORE.add_subscriber(body.phone_number)
    return {
        "masked_phone": mask_phone(record.phone_number),
        "activated_at": record.activated_at.isoformat(),
        "swap_count": len(record.swaps),
        "temporary": True,
    }


@router.post("/swaps", summary="Record a simulated SIM swap now")
def record_swap(body: SimulatorSwapRequest) -> dict:
    moment = STORE.record_swap(body.phone_number, body.at)
    return {
        "masked_phone": mask_phone(body.phone_number),
        "recorded_at": moment.isoformat(),
        "temporary": True,
    }


@router.post("/reset", summary="Reset simulation data to demo defaults")
def reset() -> dict:
    STORE.reset(seed=True)
    return {"reset": True, "subscribers": STORE.snapshot(), "temporary": True}
