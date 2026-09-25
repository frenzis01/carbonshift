"""FastAPI application: the client's HTTP surface — callback receiver,
batch-send trigger, and metrics report.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from carbonshift.services.client.app.state import advance_slot, get_current_slot, get_plan, get_plans_with_reqs_in_slot, get_requests_from_plan, store_plan
from fastapi import FastAPI, HTTPException

from .carbonshift_client import CarbonshiftError, get_stats, get_task_config, get_carbon_intensity
from .config import settings
from .models import (
    CallerCallbackPayload,
    SendBatchRequest,
    SendBatchResponse,
    SendPlanRequest,
    SendPlanResponse,
    TickRequest,
    TickResponse,
)
from .plan_runner import get_batch_from_slot, run_plan, send_slot_batch
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
    # TODO: should we worry about epoch disalignment here...?
    if (body.expected_slot != get_current_slot()):
        raise HTTPException(503, detail={"desynced": True, "expected_slot": body.expected_slot, "current_slot": get_current_slot()})
    
    current_slot = advance_slot()
    requests_submitted = 0

    # 1. work out which plan index this slot corresponds to
    # Determine the plan indexes based on the current slot
    plans_in_slot = get_plans_with_reqs_in_slot(current_slot)
    # 2. submit that index's requests to carbonshift (synchronously with caller provider)
    for plan_id in plans_in_slot:
        plan = get_plan(plan_id)
        slot_minutes = plan["slot_minutes"]
        batch = get_batch_from_slot(get_requests_from_plan(plan_id), slot_minutes, current_slot)
        send_slot_batch(tracker, batch, slot_minutes)
        requests_submitted += len(batch)
    return TickResponse(slot=current_slot, plan_index=plans_in_slot, submitted=requests_submitted)


@app.post("/run/send-batch", status_code=202)
async def run_send_batch(body: SendBatchRequest) -> SendBatchResponse:
    batch_id = send_batch(tracker, body.task, body.count, body.deadline_seconds, body.source, body.seed)
    return SendBatchResponse(batch_id=batch_id, count=body.count)


@app.post("/run/send-plan", status_code=202)
async def run_send_plan(body: SendPlanRequest) -> SendPlanResponse:
    requests_spec = [r.model_dump() for r in body.requests]
    # batch_id, slots = run_plan(tracker, requests_spec, body.slot_minutes, body.mode, body.executor_url)
    plan_id = store_plan(requests_spec, body.slot_minutes, body.mode, body.executor_url)
    return SendPlanResponse(plan_id=plan_id, count=len(requests_spec))


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
    failing the whole `/metrics/summary` response."""
    snapshot: dict[str, Any] = {"global_error_avg": None, "global_error_count": None, "tasks": {}}
    try:
        stats = get_stats()
        ci = get_carbon_intensity()
        global_error_avg = stats.get("global_error_avg")
        snapshot["global_error_avg"] = round(global_error_avg, 2) if global_error_avg is not None else None
        snapshot["global_error_count"] = stats.get("global_error_count")
        snapshot["carbon_intensity"] = ci
    except CarbonshiftError:
        logger.warning("failed to fetch carbonshift /v1/stats for metrics/summary", exc_info=True)

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
