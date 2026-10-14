"""FastAPI application: the client's HTTP surface — callback receiver,
batch-send trigger, and metrics report.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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

from .carbonshift_client import CarbonshiftError, get_qos_profile, get_stats
from .config import settings
from .qos_profile_bootstrap import register_configured_qos_profiles
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
from .timeslots import floor_to_slot, slot_of, slot_start_utc
from .tracker import RequestTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client.main")

tracker = RequestTracker(settings.metrics_path, settings.callback_timeout_seconds)

_stop_sweeper = threading.Event()
_sweeper_thread: threading.Thread | None = None
_stop_slot_ticker = threading.Event()
_slot_ticker_thread: threading.Thread | None = None
_slot_tick_lock = threading.Lock()


def _sweeper_loop() -> None:
    while not _stop_sweeper.is_set():
        tracker.sweep_timeouts()
        _stop_sweeper.wait(timeout=5.0)


def _process_slot_tick(slot: int, boundary: datetime | None = None) -> TickResponse:
    """Submit the plan batches belonging to one global slot.

    In manual mode the provider supplies `boundary`, which is simulated time.
    A realtime tick uses the same global slot arithmetic but retains wall time
    for deadline slack, so a mid-slot startup does not gain a full slot.
    """
    # Serialize provider retries with the realtime ticker so a plan batch is
    # submitted at most once for a given slot.
    with _slot_tick_lock:
        handled: list[tuple[int, int]] = []
        submitted = 0
        duplicate = False

        for plan_id, plan_index in get_plan_for_slot(slot):
            if is_slot_already_processed(plan_id, slot):
                duplicate = True
                continue

            plan = get_plan(plan_id)
            if plan is None:
                continue

            reference = plan["requests"][0]["start_at"]
            batch = get_batch_from_slot(
                get_requests_from_plan(plan_id), plan["slot_minutes"], plan_index, reference,
            )
            submitted += send_slot_batch(
                tracker,
                batch,
                plan["slot_minutes"],
                slot,
                deadline_reference=boundary if settings.manual_clock else None,
            )
            mark_slot_processed(plan_id, slot)
            handled.append((plan_id, plan_index))

        return TickResponse(slot=slot, plan_index=handled, submitted=submitted, duplicate=duplicate)


def _realtime_slot_loop() -> None:
    """Tick the current global slot and then wake at each epoch-aligned edge."""
    last_slot: int | None = None
    slot_minutes = settings.provider_slot_minutes
    while not _stop_slot_ticker.is_set():
        now = datetime.now(timezone.utc)
        current_slot = slot_of(now, slot_minutes)
        if last_slot is not None and current_slot < last_slot:
            logger.error(
                "wall clock moved backwards from slot %s to %s; waiting for it to catch up",
                last_slot,
                current_slot,
            )
            _stop_slot_ticker.wait(1.0)
            continue

        # If this thread woke late, replay every crossed slot rather than
        # shifting a plan's relative index with a private counter.
        slots = (
            [current_slot]
            if last_slot is None
            else range(last_slot + 1, current_slot + 1)
        )
        try:
            for slot in slots:
                _process_slot_tick(slot, slot_start_utc(slot, slot_minutes))
                last_slot = slot
        except Exception:
            logger.exception("realtime client tick failed at slot %s", slot)
            _stop_slot_ticker.wait(1.0)
            continue

        next_boundary = floor_to_slot(now, slot_minutes) + timedelta(minutes=slot_minutes)
        _stop_slot_ticker.wait(max(0.001, (next_boundary - now).total_seconds()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sweeper_thread, _slot_ticker_thread
    # Fail startup on permanent profile-definition errors. Transient startup
    # ordering is handled with bounded retries in the registration helper.
    register_configured_qos_profiles()
    _stop_sweeper.clear()
    _sweeper_thread = threading.Thread(target=_sweeper_loop, daemon=True, name="client-sweeper")
    _sweeper_thread.start()
    if not settings.manual_clock:
        _stop_slot_ticker.clear()
        _slot_ticker_thread = threading.Thread(
            target=_realtime_slot_loop, daemon=True, name="client-realtime-slot-ticker"
        )
        _slot_ticker_thread.start()
    logger.info("client started (carbonshift_url=%s, self_base_url=%s)",
                settings.carbonshift_url, settings.self_base_url)
    yield
    _stop_sweeper.set()
    _stop_slot_ticker.set()
    if _sweeper_thread is not None:
        _sweeper_thread.join(timeout=2)
    if _slot_ticker_thread is not None:
        _slot_ticker_thread.join(timeout=2)


app = FastAPI(title="CarbonShift Client", lifespan=lifespan)


@app.get("/health")
async def health() -> str:
    return "ok"

@app.post("/v1/tick")
def tick(body: TickRequest) -> TickResponse:
    """Handle a provider tick, or the current slot in manual emulation.

    Returns 200 with `submitted: 0` for a slot outside the plan's range — the
    provider may tick outside a plan's range, so "nothing to do" is normal.
    """
    if not settings.manual_clock:
        raise HTTPException(status_code=409, detail="CLIENT_MANUAL_CLOCK is not enabled")
    boundary = (
        datetime.fromisoformat(body.slot_start_utc.replace("Z", "+00:00"))
        if body.slot_start_utc is not None
        else slot_start_utc(body.current_slot, settings.provider_slot_minutes)
    )
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=timezone.utc)
    return _process_slot_tick(body.current_slot, boundary.astimezone(timezone.utc))


@app.post("/run/send-batch", status_code=202)
def run_send_batch(body: SendBatchRequest) -> SendBatchResponse:
    batch_id = send_batch(
        tracker,
        body.task,
        body.count,
        body.deadline_seconds,
        body.source,
        body.seed,
        body.qos_profile_id,
    )
    return SendBatchResponse(batch_id=batch_id, count=body.count)


@app.post("/run/send-plan", status_code=202)
def run_send_plan(body: SendPlanRequest) -> SendPlanResponse:
    """Register a plan, then process its current slot when running realtime."""
    if abs(body.slot_minutes - settings.provider_slot_minutes) > 1e-9:
        raise HTTPException(
            status_code=409,
            detail=(
                f"plan slot length ({body.slot_minutes} minutes) does not match "
                f"client/provider slot length ({settings.provider_slot_minutes} minutes)"
            ),
        )
    requests_spec = [r.model_dump() for r in body.requests]
    plan_id = store_plan(requests_spec, body.slot_minutes, body.mode, body.executor_url)
    plan = get_plan(plan_id)
    if not settings.manual_clock:
        # In realtime there is no provider push; do not leave the current slot
        # waiting for the next boundary just because the plan arrived mid-slot.
        current_slot = slot_of(datetime.now(timezone.utc), settings.provider_slot_minutes)
        _process_slot_tick(current_slot, slot_start_utc(current_slot, settings.provider_slot_minutes))
    return SendPlanResponse(plan_id=plan_id, count=len(requests_spec), slots=plan["slot_count"])


@app.post("/callback")
def callback(body: CallerCallbackPayload) -> dict[str, str]:
    logger.info("received callback for request_id=%s, success=%s", body.request_id, body.success)
    found = tracker.on_callback(str(body.request_id), body.success, body.result, body.error,
                                 body.actual_carbon_cost, body.actual_baseline_carbon_cost,
                                 body.execution_time_sec, body.baseline_execution_time_sec)
    if not found:
        logger.warning("callback for unknown/untracked request_id=%s", body.request_id)
    return {"status": "ok"}


@app.get("/requests")
def list_requests() -> list[dict[str, Any]]:
    return tracker.all()


@app.get("/requests/{request_id}")
def get_request(request_id: str) -> dict[str, Any]:
    t = tracker.get(request_id)
    if t is None:
        raise HTTPException(status_code=404, detail="unknown request_id")
    return t.to_dict()


@app.get("/metrics/summary")
def metrics_summary() -> dict[str, Any]:
    summary = tracker.summary()
    summary["scheduler"] = _scheduler_snapshot()
    return summary


def _scheduler_snapshot() -> dict[str, Any]:
    """Live state fetched from carbonshift (not derived from tracked
    requests): descriptive fleet-wide error telemetry, the selected profiles'
    declared QoS policies. They are reported by profile ID because two
    profiles for one task may have different thresholds or error semantics.
    Degrades to
    `null`s if Carbonshift is unreachable rather than failing `/metrics/summary`.

    Carbon intensity now comes from the **provider**, not carbonshift. The
    provider owns the manual clock and serves readings; realtime clients derive
    slot edges locally. `observed` is the measurement taken for the current
    slot, which is `null` until that slot has been reached — a measurement of a
    future slot does not exist.
    """
    snapshot: dict[str, Any] = {
        "global_error_avg": None,
        "global_error_count": None,
        "profiles": {},
    }
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

    profile_ids = sorted({
        item["qos_profile_id"]
        for item in tracker.all()
        if item.get("qos_profile_id")
    })
    for profile_id in profile_ids:
        try:
            profile = get_qos_profile(profile_id)
            snapshot["profiles"][profile_id] = {
                "task_kind": profile["task_kind"],
                "error_semantics": profile["error_semantics"],
                "max_error_threshold": profile["max_error_threshold"],
                "error_window": profile["error_window"],
                "cumulative_error": profile["cumulative_error"],
            }
        except CarbonshiftError:
            logger.warning(
                "failed to fetch carbonshift QoS profile=%s for metrics/summary",
                profile_id,
                exc_info=True,
            )
            snapshot["profiles"][profile_id] = None
    return snapshot


@app.get("/metrics/progress")
async def metrics_progress() -> dict[str, Any]:
    return tracker.progress()
