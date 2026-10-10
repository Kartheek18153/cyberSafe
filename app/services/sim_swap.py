"""Business logic: normalize provider outcomes into risk signals.

Pinned spec: CAMARA SimSwap API v2.1.0 (camaraproject/SimSwap r3.2/r3.3).

Three outcomes only:
  completed + detected   -> risk_signal=recent_sim_change
  completed + no swap    -> risk_signal=no_recent_change
  unknown/unsupported    -> risk_signal=check_unavailable/unsupported_operation

A provider failure NEVER becomes sim_swap_detected=False.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.config import Settings
from app.schemas import SimSwapResult, mask_phone
from app.services.providers.base import (
    ProviderError,
    SimSwapProvider,
    UnsupportedCapability,
)


class SimSwapService:
    def __init__(self, provider: SimSwapProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    def _base(
        self,
        *,
        status: str,
        phone_number: Optional[str],
        lookback_hours: Optional[int],
        detail: Optional[str] = None,
    ) -> dict:
        return {
            "status": status,  # type: ignore[typeddict]
            "provider": getattr(self.provider, "name", self.settings.provider),
            "lookback_hours": lookback_hours,
            "checked_at": datetime.now(timezone.utc),
            "masked_phone": mask_phone(phone_number),
            "detail": detail,
            "evidence": [],
        }

    async def check(
        self, *, phone_number: Optional[str], lookback_hours: int
    ) -> SimSwapResult:
        if not self.provider.supports_check:
            base = self._base(
                status="unsupported",
                phone_number=phone_number,
                lookback_hours=lookback_hours,
                detail="Provider does not support SIM Swap check.",
            )
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="unsupported_operation",
            )
        try:
            outcome = await self.provider.check(
                phone_number=phone_number, max_age_hours=lookback_hours
            )
        except UnsupportedCapability as exc:
            base = self._base(
                status="unsupported",
                phone_number=phone_number,
                lookback_hours=lookback_hours,
                detail=str(exc),
            )
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="unsupported_operation",
            )
        except ProviderError as exc:
            base = self._base(
                status="unknown",
                phone_number=phone_number,
                lookback_hours=lookback_hours,
                detail=f"{type(exc).__name__}: {exc}",
            )
            base["evidence"] = [
                {"type": "provider_error", "error": type(exc).__name__}
            ]
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="check_unavailable",
            )
        base = self._base(
            status="completed", phone_number=phone_number, lookback_hours=lookback_hours
        )
        evidence: dict = {
            "type": "sim_swap_check",
            "swapped": outcome.swapped,
            "max_age_hours": outcome.max_age_hours,
            "http_status": outcome.http_status,
        }
        if outcome.x_correlator:
            evidence["x_correlator"] = outcome.x_correlator
        base["evidence"] = [evidence]
        # Calibrated verdicts, encoded in the payload so downstream consumers
        # cannot mistake a signal for proof (or absence for safety).
        if outcome.swapped:
            base["detail"] = (
                f"SIM swap reported within the last {lookback_hours}h. "
                "Treat as a risk signal, not proof of fraud."
            )
        else:
            base["detail"] = (
                f"No SIM swap reported within the last {lookback_hours}h. "
                "This does not mean the number is safe."
            )
        return SimSwapResult(
            **base,
            sim_swap_detected=outcome.swapped,
            last_swap_at=None,
            risk_signal="recent_sim_change" if outcome.swapped else "no_recent_change",
        )

    async def retrieve_date(self, *, phone_number: Optional[str]) -> SimSwapResult:
        if not self.provider.supports_retrieve_date:
            base = self._base(
                status="unsupported",
                phone_number=phone_number,
                lookback_hours=None,
                detail="Provider does not support retrieve-date.",
            )
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="unsupported_operation",
            )
        try:
            outcome = await self.provider.retrieve_date(phone_number=phone_number)
        except UnsupportedCapability as exc:
            base = self._base(
                status="unsupported",
                phone_number=phone_number,
                lookback_hours=None,
                detail=str(exc),
            )
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="unsupported_operation",
            )
        except ProviderError as exc:
            base = self._base(
                status="unknown",
                phone_number=phone_number,
                lookback_hours=None,
                detail=f"{type(exc).__name__}: {exc}",
            )
            base["evidence"] = [
                {"type": "provider_error", "error": type(exc).__name__}
            ]
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="check_unavailable",
            )
        # Recency vs default window is informational only; a null date WITH a
        # monitoredPeriod means "no swap in monitored window" (completed).
        # A null date WITHOUT monitoredPeriod (always the case for v1, which
        # has no monitoredPeriod) means the operator could not communicate
        # retention — it must NOT become a negative result. Map to unknown.
        if outcome.latest_sim_change is None and outcome.monitored_period_days is None:
            base = self._base(
                status="unknown",
                phone_number=phone_number,
                lookback_hours=None,
                detail=(
                    "Provider returned null latestSimChange without "
                    "monitoredPeriod; operator retention is unknown, so no "
                    "recency verdict is possible. This is not evidence of safety."
                ),
            )
            base["evidence"] = [
                {
                    "type": "sim_swap_retrieve_date",
                    "latest_sim_change": None,
                    "monitored_period_days": None,
                    "http_status": outcome.http_status,
                }
            ]
            if outcome.x_correlator:
                base["evidence"][0]["x_correlator"] = outcome.x_correlator
            return SimSwapResult(
                **base,
                sim_swap_detected=None,
                last_swap_at=None,
                risk_signal="check_unavailable",
            )
        base = self._base(
            status="completed", phone_number=phone_number, lookback_hours=None
        )
        retrieve_evidence: dict = {
            "type": "sim_swap_retrieve_date",
            "latest_sim_change": outcome.latest_sim_change.isoformat()
            if outcome.latest_sim_change
            else None,
            "monitored_period_days": outcome.monitored_period_days,
            "http_status": outcome.http_status,
        }
        if outcome.x_correlator:
            retrieve_evidence["x_correlator"] = outcome.x_correlator
        base["evidence"] = [retrieve_evidence]
        detected: Optional[bool] = None
        signal = "no_recent_change"
        if outcome.latest_sim_change is not None:
            age_hours = (
                datetime.now(timezone.utc) - outcome.latest_sim_change
            ).total_seconds() / 3600
            detected = age_hours <= self.settings.default_lookback_hours
            signal = "recent_sim_change" if detected else "no_recent_change"
        else:
            # Only reachable for null WITH monitoredPeriod (null without it
            # returns unknown above): no swap events in the monitored window.
            detected = False
        return SimSwapResult(
            **base,
            sim_swap_detected=detected,
            last_swap_at=outcome.latest_sim_change,
            monitored_period_days=outcome.monitored_period_days,
            risk_signal=signal,  # type: ignore[arg-type]
        )
