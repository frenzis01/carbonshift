"""Pydantic request/response schemas for the client's HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel


class CallerCallbackPayload(BaseModel):
    """Body carbonshift POSTs to `/callback` (its `CallerCallbackPayload`,
    see carbonshift/rust/src/service/models.rs) — `result` is whatever the
    executor put there (task/flavour/model/output/confidence/quality_score)."""

    request_id: int
    success: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    actual_carbon_cost: Optional[float] = None
    actual_baseline_carbon_cost: Optional[float] = None
    execution_time_seconds: Optional[float] = None
    baseline_execution_time_seconds: Optional[float] = None


class SendBatchRequest(BaseModel):
    task: Literal["text_generation", "ner", "question_answering"] = "text_generation"
    count: int = 5
    deadline_seconds: float = 30.0
    # "synthetic": a handful of hardcoded examples, no download needed.
    # "dataset": pulls real examples from a HuggingFace dataset (see README).
    source: Literal["synthetic", "dataset"] = "synthetic"
    seed: int = 42


class SendBatchResponse(BaseModel):
    batch_id: str
    count: int


class PlanRequestSpec(BaseModel):
    """One request in a timeslot-aware plan: absolute (dated) start/deadline
    instants, discretized into timeslots by `SendPlanRequest.slot_minutes`."""

    task: Literal["text_generation", "ner", "question_answering"]
    input: dict[str, Any]
    start_at: datetime
    deadline_at: datetime


class SendPlanRequest(BaseModel):
    requests: list[PlanRequestSpec]
    slot_minutes: float = 30.0
    # "realtime": waits for each request's real `start_at` wall-clock time.
    # "emulated": fires each timeslot's requests immediately, then
    # synchronizes via carbonshift's and the executor's `/admin/advance-slot`
    # (see README "Emulazione a tempo fittizio") instead of waiting for real
    # time to pass between slots.
    mode: Literal["realtime", "emulated"] = "realtime"
    executor_url: Optional[str] = None


class SendPlanResponse(BaseModel):
    plan_id: int
    count: int
    slots: int


class TickRequest(BaseModel):
    """Body the provider POSTs on each rollover.

    Mirrors the provider's push payload field-for-field (see
    `provider/INTERFACE.md` §"Notification body"). Do not invent field names
    here: the provider's payload *is* the contract, and a second, differently
    named model is how the two services drift apart.

    `current_slot` is a **global** slot (epoch-anchored), not a plan index —
    see `state.get_plan_for_slot` for the translation.
    """

    current_slot: int
    slot_start_utc: str
    source: str = ""
    observed: Optional[dict[str, Any]] = None
    forecast: list[dict[str, Any]] = []


class TickResponse(BaseModel):
    slot: int
    #: (plan_id, plan_index) pairs handled for this tick.
    plan_index: list[tuple[int, int]] = []
    submitted: int = 0
    #: True when this tick was a retry of one already handled (idempotency).
    duplicate: bool = False
    