"""Provider abstraction: keeps CAMARA HTTP details out of business logic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class ProviderError(Exception):
    """Base class for all provider failures. Never maps to a negative result."""


class ProviderNotConfigured(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderTimeout(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    def __init__(self, message: str = "Rate limited by provider", retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    pass


class ProviderBadResponse(ProviderError):
    pass


class UnsupportedCapability(ProviderError):
    pass


@dataclass
class CheckOutcome:
    swapped: bool
    max_age_hours: int
    x_correlator: Optional[str] = None
    http_status: Optional[int] = None


@dataclass
class RetrieveDateOutcome:
    latest_sim_change: Optional[datetime]
    monitored_period_days: Optional[int] = None
    x_correlator: Optional[str] = None
    http_status: Optional[int] = None


class SimSwapProvider(ABC):
    """Interface every provider (live or mock) must implement."""

    name: str = "base"

    supports_check: bool = True
    supports_retrieve_date: bool = True

    @abstractmethod
    async def check(
        self, *, phone_number: Optional[str], max_age_hours: int
    ) -> CheckOutcome:
        raise NotImplementedError

    @abstractmethod
    async def retrieve_date(
        self, *, phone_number: Optional[str]
    ) -> RetrieveDateOutcome:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
