"""Pydantic HTTP schemas for the provider's public API (see INTERFACE.md)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ForecastPointModel(BaseModel):
    """A forecasted value. Deliberately has no `actual` field — a forecast
    spans many slots, a measurement concerns exactly one (see `ObservedPoint`)."""

    slot: int
    forecast: float


class ObservedPointModel(BaseModel):
    """A reading that was actually taken, at a known moment."""

    slot: int
    actual: float
    observed_at_slot: int


class ForecastResponse(BaseModel):
    """Body of `GET /v1/forecast` — a forecast window, never a measurement."""

    source: str
    slot_minutes: float
    current_slot: int
    points: list[ForecastPointModel]


class CurrentSlotResponse(BaseModel):
    current_slot: int
    slot_start_utc: str
    slot_minutes: float
    manual_clock: bool
    local_step: int
    source: str


class AdvanceRequest(BaseModel):
    """Body of `POST /v1/advance-slot`.

    `expect_slot`, when supplied, is a safety interlock: the advance is
    rejected if the provider's current slot is not the expected one. That is
    what makes a retried rollover notification idempotent instead of
    double-advancing the whole system.
    """

    expect_slot: Optional[int] = None
    # Allow an operator/test to advance without fanning out to peers (e.g. to
    # probe the provider in isolation).
    notify_peers: bool = True


class AdvanceResponse(BaseModel):
    current_slot: int
    slot_start_utc: str
    local_step: int
    source: str
    notified: bool
    all_ok: bool
    deliveries: list[dict]


class MetaResponse(BaseModel):
    role: str
    source: str
    slot_minutes: float
    epoch_utc: str
    forecast_horizon_slots: int
    manual_clock: bool
    auto_advance_clock: bool
    peers: list[dict]
