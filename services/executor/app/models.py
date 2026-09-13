"""Pydantic request/response schemas for the executor's HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel


class JobSubmitRequest(BaseModel):
    """Body of `POST /jobs` — the executor's native, standalone API."""

    request_id: Optional[str] = None
    task: Literal["text_generation", "ner", "question_answering"]
    flavour: Literal["accurate", "balanced", "fast"]
    input: dict[str, Any]
    # Absolute UTC timestamp to run at; omitted = run as soon as possible.
    execute_at: Optional[datetime] = None
    callback_url: Optional[str] = None


class JobSubmitResponse(BaseModel):
    request_id: str
    status: str
    execute_at: datetime
    queue_position: int


class CarbonshiftDispatchPayload(BaseModel):
    """Body of `POST /dispatch` — matches carbonshift's `ExecutorDispatchPayload`
    (see carbonshift/rust/src/service/models.rs) verbatim, so `EXECUTOR_URL`
    can point straight at this executor with no changes on either side.
    `payload` is carbonshift's opaque, caller-supplied field; the executor
    expects it to contain `{"task": ..., "input": {...}}` (and optionally
    `"execute_at"`, `"reference"`/`"reference_answer"`/`"reference_entities"`
    for quality scoring).
    """

    request_id: int
    scheduled_slot: int
    flavour: str
    carbon_cost: float
    callback_url: str
    payload: dict[str, Any]
