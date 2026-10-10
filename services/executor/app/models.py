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


'''
pub struct AdvanceSlotBody {
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub current_slot: i64,
    #[serde(default)]
    pub slot_start_utc: Option<String>,
    // observed is a dict with "slot", "observed_at_slot", and "actual" keys
    // the latter being the actual observed carbon intensity.
    #[serde(default)]
    pub observed: Option<ObservedPoint>,
    // forecast is a list of dictionaries of slot + forecast value
    #[serde(default)]
    // pub forecast: Option<std::collections::HashMap<usize, f64>>,
    pub forecast: Option<Vec<ForecastPoint>>,
}

// TODO: is Clone ok? Does it actually work?
#[derive(Debug, Deserialize, Clone)]
pub struct ForecastPoint {
    pub slot: i64,
    pub forecast: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ObservedPoint {
    pub slot: i64,
    pub observed_at_slot: i64,
    pub actual: f64,
}'''

class ForecastPoint(BaseModel):
    slot: int
    forecast: float
    
class ObservedPoint(BaseModel):
    slot: int
    observed_at_slot: int
    actual: float
    
class AdvanceSlotPayload(BaseModel):
    source: str = ""
    kind: str = "rollover"
    current_slot: Optional[int] = None
    slot_minutes: Optional[float] = None
    slot_start_utc: Optional[str] = None
    observed: Optional[ObservedPoint] = None
    forecast: Optional[list[ForecastPoint]] = None

class CarbonshiftDispatchPayload(BaseModel):
    """Body of `POST /dispatch` — matches carbonshift's `ExecutorDispatchPayload`
    (see carbonshift/rust/src/service/models.rs) verbatim, so `EXECUTOR_URL`
    can point straight at this executor with no changes on either side.
    `payload` is carbonshift's opaque, caller-supplied field; the executor
    expects it to contain `{"task": ..., "input": {...}}` (and optionally
    `"reference"`/`"reference_answer"`/`"reference_entities"` for quality scoring).
    """

    request_id: int
    scheduled_slot: int
    execute_at: datetime
    flavour: str
    carbon_cost: float
    callback_url: str
    payload: dict[str, Any]
