"""FastAPI application: the client's HTTP surface — callback receiver,
batch-send trigger, and metrics report.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from .state import (
    get_plan,
    get_plan_for_slot,
    get_requests_from_plan,
    is_slot_already_processed,
    mark_slot_processed,
    store_plan,
)

from fastapi import FastAPI, HTTPException

from .carbonshift_client import CarbonshiftError, get_stats, get_task_config
from .config import settings
from . import provider_client
from .provider_client import ProviderError
from .models import (
    CallerCallbackPayload,
    SendBatchRequest,
    SendBatchResponse,
    SendPlanRequest,
    SendPlanResponse,
    TickRequest,
    TickResponse,
)
from .plan_runner import get_batch_from_slot, send_slot_batch
from .runner import send_batch
from .tracker import RequestTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client.main")

tracker = RequestTracker(settings.metrics_path, settings.callback_timeout_seconds)

_stop_sweeper = threading.Event()
_sweeper_thread: threading.Thread | None = None


def _sweeper_loop() -> None:
    while not _stop_sweeper.is_set():
        tracker.sweep_timeouts()
        _stop_sweeper.wait(timeout=5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sweeper_thread
    _stop_sweeper.clear()
    _sweeper_thread = threading.Thread(target=_sweeper_loop, daemon=True, name="client-sweeper")
    _sweeper_thread.start()
    logger.info("client started (carbonshift_url=%s, self_base_url=%s)",
                settings.carbonshift_url, settings.self_base_url)
    yield
    _stop_sweeper.set()
    if _sweeper_thread is not None:
        _sweeper_thread.join(timeout=2)


app = FastAPI(title="CarbonShift Client", lifespan=lifespan)


@app.get("/health")
async def health() -> str:
    return "ok"

@app.post("/v1/tick")
async def tick(body: TickRequest) -> TickResponse:
    """The provider tells us the system has entered `body.current_slot`.

    We answer with that slot's work. The client keeps **no clock of its own**:
    the position is always derived from the slot we are told about, so a
    retried or missed tick cannot make us drift out of step with the provider.

    Returns 200 with `submitted: 0` for a slot outside the plan's range — the
    provider ticks on its own schedule, which need not coincide with the
    plan's, so "nothing to do" is a normal outcome, not an error.
    """
    slot = body.current_slot
    handled: list[tuple[int, int]] = []
    submitted = 0
    duplicate = False

    for plan_id, plan_index in get_plan_for_slot(slot):
        # Idempotency: the provider retries on failure, so a tick we already
        # processed must be a no-op rather than a double submission.
        if is_slot_already_processed(plan_id, slot):
            duplicate = True
            continue

        plan = get_plan(plan_id)
        if plan is None:  # cleared between the two lookups
            continue

        # The plan's own start instant is the reference for grouping, so the
        # index is measured from a slot boundary rather than from a jittered
        # mid-slot `min(start_at)`. `store_plan` normalised it to a datetime.
        reference = plan["requests"][0]["start_at"]
        batch = get_batch_from_slot(
            get_requests_from_plan(plan_id), plan["slot_minutes"], plan_index, reference,
        )
        submitted += send_slot_batch(tracker, batch, plan["slot_minutes"])
        mark_slot_processed(plan_id, slot)
        handled.append((plan_id, plan_index))

    return TickResponse(slot=slot, plan_index=handled, submitted=submitted, duplicate=duplicate)


@app.post("/run/send-batch", status_code=202)
async def run_send_batch(body: SendBatchRequest) -> SendBatchResponse:
    batch_id = send_batch(tracker, body.task, body.count, body.deadline_seconds, body.source, body.seed)
    return SendBatchResponse(batch_id=batch_id, count=body.count)


@app.post("/run/send-plan", status_code=202)
async def run_send_plan(body: SendPlanRequest) -> SendPlanResponse:
    """Register a plan. It is *stored*, not started: the provider's ticks drive it."""
    requests_spec = [r.model_dump() for r in body.requests]
    plan_id = store_plan(requests_spec, body.slot_minutes, body.mode, body.executor_url)
    plan = get_plan(plan_id)
    return SendPlanResponse(plan_id=plan_id, count=len(requests_spec), slots=plan["slot_count"])


@app.post("/callback")
async def callback(body: CallerCallbackPayload) -> dict[str, str]:
    logger.info("received callback for request_id=%s, success=%s", body.request_id, body.success)
    found = tracker.on_callback(str(body.request_id), body.success, body.result, body.error,
                                 body.actual_carbon_cost, body.actual_baseline_carbon_cost,
                                 body.execution_time_seconds, body.baseline_execution_time_seconds)
    if not found:
        logger.warning("callback for unknown/untracked request_id=%s", body.request_id)
    return {"status": "ok"}


@app.get("/requests")
async def list_requests() -> list[dict[str, Any]]:
    return tracker.all()


@app.get("/requests/{request_id}")
async def get_request(request_id: str) -> dict[str, Any]:
    t = tracker.get(request_id)
    if t is None:
        raise HTTPException(status_code=404, detail="unknown request_id")
    return t.to_dict()


@app.get("/metrics/summary")
async def metrics_summary() -> dict[str, Any]:
    summary = tracker.summary()
    summary["scheduler"] = _scheduler_snapshot()
    return summary


def _scheduler_snapshot() -> dict[str, Any]:
    """Live state fetched from carbonshift (not derived from tracked
    requests): the scheduler's own global error average (task-agnostic by
    design — see PLAN_SERVICE.md) and each seen task's *declared*
    `max_error_threshold`, so it's clear whether the run stayed within
    budget. Degrades to `null`s if carbonshift is unreachable, rather than
    failing the whole `/metrics/summary` response.

    Carbon intensity now comes from the **provider**, not carbonshift: the
    provider owns the clock and the readings (see provider/ARCHITECTURE.md
    §10). `observed` is the measurement taken for the current slot, which is
    `null` until that slot has been reached — a measurement of a future slot
    does not exist.
    """
    snapshot: dict[str, Any] = {"global_error_avg": None, "global_error_count": None, "tasks": {}}
    try:
        stats = get_stats()
        global_error_avg = stats.get("global_error_avg")
        snapshot["global_error_avg"] = round(global_error_avg, 2) if global_error_avg is not None else None
        snapshot["global_error_count"] = stats.get("global_error_count")
    except CarbonshiftError:
        logger.warning("failed to fetch carbonshift /v1/stats for metrics/summary", exc_info=True)

    try:
        snapshot["carbon_intensity"] = provider_client.get_observed()
    except ProviderError:
        logger.warning("failed to fetch provider /v1/observed for metrics/summary", exc_info=True)
        snapshot["carbon_intensity"] = None

    for task in sorted({item["task"] for item in tracker.all()}):
        try:
            cfg = get_task_config(task)
            threshold = cfg.get("max_error_threshold")
            capacity_tiers = cfg.get("capacity_tiers")
            snapshot["tasks"][task] = {"max_error_threshold": round(threshold, 2) if threshold is not None else None,
                                        "capacity_tiers": capacity_tiers}
        except CarbonshiftError:
            logger.warning("failed to fetch carbonshift task config for task=%s", task, exc_info=True)
            snapshot["tasks"][task] = {"max_error_threshold": None,
                                        "capacity_tiers": None}
    return snapshot


@app.get("/metrics/progress")
async def metrics_progress() -> dict[str, Any]:
    return tracker.progress()
