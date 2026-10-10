"""Pydantic schemas: external API validation + normalized internal response."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Source of truth: SIM Swap v1 Swagger schemas supplied from the GSMA sandbox
# portal (SimSwapInfo, CheckSimSwapInfo, PhoneNumber, CreateCheckSimSwap,
# CreateSimSwapDate, ErrorInfo; server template {apiRoot}/sim-swap/v1).
# v2.1.0 (camaraproject/SimSwap r3.2/r3.3) shares identical limits, so
# validation below is version-agnostic; per-version phone-identifier rules
# live in the provider adapters (strict v2 vs lenient v1).
E164_RE = re.compile(r"^\+[1-9][0-9]{4,14}$")

# CreateCheckSimSwap.maxAge: integer 1..2400 hours. The v1/v2 specification
# default when the field is omitted is 240; this application's /check endpoint
# instead defaults lookback_hours to 72 (hackathon MVP risk window) and always
# sends maxAge explicitly, so the spec default never triggers silently.
CAMARA_MIN_MAX_AGE = 1
CAMARA_MAX_MAX_AGE = 2400
SPEC_DEFAULT_MAX_AGE = 240
APP_DEFAULT_LOOKBACK_HOURS = 72


def mask_phone(phone: Optional[str]) -> Optional[str]:
    """Return a log/response-safe masked phone, e.g. +34******3334."""
    if not phone:
        return None
    if len(phone) <= 6:
        return "***"
    return f"{phone[:3]}******{phone[-4:]}"


def validate_e164(phone: str) -> str:
    if not E164_RE.match(phone):
        raise ValueError("phone_number must be E.164 format, e.g. +346661113334")
    return phone


class CheckRequest(BaseModel):
    """POST /api/security/sim-swap/check body.

    Mirrors CreateCheckSimSwap (v1 source of truth): optional E.164
    ``phoneNumber`` + integer ``maxAge`` hours 1..2400. This app defaults
    ``lookback_hours`` to 72 (MVP window) rather than the spec default 240,
    and always forwards it as an explicit ``maxAge``.
    phone_number: required for 2-legged flows; v2 forbids it for token-bound
    3-legged flows while v1 passes it through for server-side validation.
    We accept it as optional and let the provider adapter enforce the rule.
    """

    phone_number: Optional[str] = Field(
        default=None, description="E.164 phone number, e.g. +346661113334"
    )
    lookback_hours: int = Field(
        default=APP_DEFAULT_LOOKBACK_HOURS, ge=CAMARA_MIN_MAX_AGE, le=CAMARA_MAX_MAX_AGE
    )

    @field_validator("phone_number")
    @classmethod
    def _check_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_e164(v.strip())


class RetrieveDateRequest(BaseModel):
    phone_number: Optional[str] = Field(
        default=None, description="E.164 phone number, e.g. +346661113334"
    )

    @field_validator("phone_number")
    @classmethod
    def _check_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_e164(v.strip())


class SimulatorSwapRequest(BaseModel):
    """Body for POST /api/simulator/swaps (temporary simulation data only)."""

    phone_number: str = Field(description="E.164 phone number, e.g. +346661113334")
    at: Optional[datetime] = Field(
        default=None,
        description="Event timestamp (timezone-aware). Defaults to now.",
    )

    @field_validator("phone_number")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        return validate_e164(v.strip())


RiskSignal = Literal[
    "recent_sim_change",
    "no_recent_change",
    "check_unavailable",
    "unsupported_operation",
]

CheckStatus = Literal["completed", "unknown", "unsupported"]


class SimSwapResult(BaseModel):
    status: CheckStatus
    provider: str
    lookback_hours: Optional[int] = None
    sim_swap_detected: Optional[bool] = None
    last_swap_at: Optional[datetime] = None
    monitored_period_days: Optional[int] = None
    risk_signal: RiskSignal
    evidence: list[dict] = Field(default_factory=list)
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    masked_phone: Optional[str] = None
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    provider: str
    auth_flow: str
    live_configured: bool
    missing_config: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
