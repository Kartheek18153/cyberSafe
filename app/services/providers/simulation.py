"""Stateful SIM-swap simulation with temporary in-memory data.

This is a *functioning* simulation, not a fixed script: it keeps a temporary
subscriber registry (phone -> activation + swap-event history) that you mutate
through the simulator management endpoints while the server runs. The SIM Swap
/check and /retrieve-date operations then react to that temp data exactly like
they would to operator data (same validation, same risk mapping).

Volatile by design: everything lives in process memory, nothing is persisted,
and a restart reseeds the two demo numbers. Never mix with live mode — the
factory only selects this provider for OG_AUTH_FLOW=simulation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import Settings
from app.schemas import mask_phone
from app.services.providers.base import (
    CheckOutcome,
    ProviderUnavailable,
    RetrieveDateOutcome,
    SimSwapProvider,
)


@dataclass
class SubscriberRecord:
    phone_number: str
    activated_at: datetime
    swaps: list[datetime] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SimulationStore:
    """Thread-safe temporary registry. All data is fake and disposable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, SubscriberRecord] = {}

    def reset(self, *, seed: bool = True) -> None:
        with self._lock:
            self._subscribers = {}
        if seed:
            self.seed_demo_data()

    def seed_demo_data(self) -> None:
        from datetime import timedelta

        now = _now()
        with self._lock:
            self._subscribers["+346661113334"] = SubscriberRecord(
                phone_number="+346661113334",
                activated_at=now - timedelta(days=90),
                swaps=[],
            )
            self._subscribers["+3412340000"] = SubscriberRecord(
                phone_number="+3412340000",
                activated_at=now - timedelta(days=90),
                swaps=[now - timedelta(hours=5)],
            )

    def add_subscriber(self, phone_number: str) -> SubscriberRecord:
        with self._lock:
            record = self._subscribers.get(phone_number)
            if record is None:
                record = SubscriberRecord(
                    phone_number=phone_number, activated_at=_now(), swaps=[]
                )
                self._subscribers[phone_number] = record
            return SubscriberRecord(
                phone_number=record.phone_number,
                activated_at=record.activated_at,
                swaps=list(record.swaps),
            )

    def record_swap(self, phone_number: str, at: Optional[datetime] = None) -> datetime:
        moment = at or _now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        with self._lock:
            record = self._subscribers.get(phone_number)
            if record is None:
                record = SubscriberRecord(
                    phone_number=phone_number, activated_at=moment, swaps=[]
                )
                self._subscribers[phone_number] = record
            record.swaps.append(moment)
            return moment

    def get(self, phone_number: str) -> Optional[SubscriberRecord]:
        with self._lock:
            record = self._subscribers.get(phone_number)
            if record is None:
                return None
            return SubscriberRecord(
                phone_number=record.phone_number,
                activated_at=record.activated_at,
                swaps=list(record.swaps),
            )

    def snapshot(self) -> list[dict]:
        """Masked overview for the management endpoint. Never full numbers."""
        with self._lock:
            return [
                {
                    "masked_phone": mask_phone(phone),
                    "swap_count": len(record.swaps),
                    "last_event_at": (
                        max(record.swaps).isoformat() if record.swaps else None
                    ),
                }
                for phone, record in sorted(self._subscribers.items())
            ]


STORE = SimulationStore()
STORE.seed_demo_data()


class SimulatedSimSwapProvider(SimSwapProvider):
    """v1-shaped provider backed by the temporary simulation store.

    Semantics mirror a real operator: only recorded swap events count toward
    /check; /retrieve-date returns the latest swap, else the activation date
    (v1 default), else unknown for numbers the registry never saw.
    """

    name = "simulation"

    def __init__(
        self, settings: Optional[Settings] = None, store: Optional[SimulationStore] = None
    ) -> None:
        self._settings = settings
        self._store = store or STORE

    async def check(
        self, *, phone_number: Optional[str], max_age_hours: int
    ) -> CheckOutcome:
        record = self._store.get(phone_number or "")
        if record is None:
            raise ProviderUnavailable(
                "Simulated registry has no such subscriber."
            )
        now = _now()
        swapped = any(
            (now - event).total_seconds() / 3600 <= max_age_hours
            for event in record.swaps
        )
        return CheckOutcome(swapped=swapped, max_age_hours=max_age_hours)

    async def retrieve_date(
        self, *, phone_number: Optional[str]
    ) -> RetrieveDateOutcome:
        record = self._store.get(phone_number or "")
        if record is None:
            raise ProviderUnavailable(
                "Simulated registry has no such subscriber."
            )
        if record.swaps:
            return RetrieveDateOutcome(latest_sim_change=max(record.swaps))
        # v1 default: activation date when no swap was ever performed.
        return RetrieveDateOutcome(latest_sim_change=record.activated_at)
