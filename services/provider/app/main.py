"""FastAPI application for the carbon-intensity provider.

Two roles, one contract (see INTERFACE.md / ARCHITECTURE.md):

* `PROVIDER_ROLE=local`  → synthesise a forecast in-process (emulation,
  offline tests, and the current default).
* `PROVIDER_ROLE=remote` → poll a real upstream (⚠️ stub, see source.py).

Endpoints
---------
`GET  /health`            liveness + which adapter is live + clock drift
`GET  /v1/meta`           full configuration actually in effect
`GET  /v1/slot`           the current slot (cheap; safe to poll)
`GET  /v1/forecast`       `?from_slot=`/`?count=` forecast window
`GET  /v1/observed`       `?slot=` the reading taken for a reached slot
`POST /v1/advance-slot`   manual-clock only: drain the active slot, then roll
`POST /v1/forecast`       not implemented (forecast is not caller-supplied)

The provider is intentionally stateless apart from its manual clock and the
readings it has taken. Realtime slots are derived from wall time; restarting a
manual emulation resets its virtual start to today's local midnight, so restart
the whole emulated stack together.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import notifications
from .clock import ProviderClock
from .config import settings
from .models import (
    AdvanceRequest,
    AdvanceResponse,
    CurrentSlotResponse,
    ForecastPointModel,
    ForecastResponse,
    MetaResponse,
    ObservedPointModel,
)
from .source import ObservedPoint, build_source
from .slots import floor_to_slot, local_midnight_utc, parse_epoch, slot_of

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("provider.main")

epoch = parse_epoch(settings.epoch_iso)
cycle_origin = local_midnight_utc()
emulation_start = cycle_origin if settings.manual_clock else None
if floor_to_slot(cycle_origin, settings.slot_minutes, epoch) != cycle_origin:
    raise ValueError(
        "local midnight is not aligned to the configured slot grid; "
        "use a slot duration and timezone whose boundaries include midnight"
    )

# Keep the fixed epoch for globally comparable slot IDs. Emulation changes
# only its virtual start instant, not the shared timestamp/numbering contract.
source = build_source(
    settings,
    cycle_origin_slot=slot_of(cycle_origin, settings.slot_minutes, epoch),
)
clock = ProviderClock(
    manual=settings.manual_clock,
    slot_minutes=settings.slot_minutes,
    epoch=epoch,
    start_at=emulation_start,
)
peers = notifications.peers_from_settings(settings)

#: Readings actually taken, keyed by the slot they describe. Manual mode
#: records them during rollover; realtime mode lazily records the current slot.
observed_readings: dict[int, ObservedPoint] = {}

#: Set once a fan-out partially fails. After that the run is unrecoverable:
#: the clock moved but not every peer followed, and there is no safe way to
#: resynchronize automatically (see `_mark_desynced`). Every subsequent
#: advance is refused until the process is restarted, so a desynced run can
#: never quietly continue and produce plausible-looking metrics.
_desynced: dict | None = None
_advance_lock = threading.Lock()


def _mark_desynced(report: notifications.RolloverReport) -> None:
    global _desynced
    if _desynced is None:
        _desynced = report.to_dict()
        logger.error(
            "DESYNCED: slot %s advanced but not all peers acknowledged; "
            "refusing further advances until restart. %s",
            report.tick.global_slot if report.tick else None,
            _desynced,
        )


def is_desynced() -> bool:
    return _desynced is not None

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "provider started role=%s source=%s slot_minutes=%s manual_clock=%s",
        settings.role,
        source.name,
        settings.slot_minutes,
        clock.manual,
    )
    if source.name == "remote" and abs(settings.slot_minutes - 30.0) > 1e-9:
        # Loud, because it would otherwise produce a plausibly-wrong series:
        # the upstream is natively half-hourly, so any other slot length needs
        # an explicit resampling policy (see ARCHITECTURE.md §9).
        logger.warning(
            "PROVIDER_SLOT_MINUTES=%s with the remote source: the GB upstream is "
            "natively half-hourly, so slots do not map 1:1 and a resampling policy "
            "is required",
            settings.slot_minutes,
        )
    yield


app = FastAPI(title="CarbonShift Carbon Intensity Provider", lifespan=lifespan)

def announce(notify: bool) -> notifications.RolloverReport:
    """Tell every peer which slot we are in, without crossing a boundary.

    This is the "hello": it lets each peer align its own clock and seed its
    forecast from the same instant, before any slot has been crossed. Without
    it, the first slot the peers ever hear about is the one *after* the one
    the provider started in, and that starting slot is never described.

    Consumers are synchronized before the client is ticked. That guarantees
    CarbonShift has installed its global-slot offset before the initial plan
    batch arrives; unlike a rollover, this announcement does not dispatch new
    slot work, so producer-first ordering is not needed here.
    """
    current = clock.current_slot()
    reading = source.observe(current)
    if reading is not None:
        observed_readings[current] = reading
    payload = notifications.build_rollover_payload(
        clock, source, horizon_slots=settings.forecast_horizon_slots, reading=reading,
    )
    payload["kind"] = "announce"
    deliveries = []
    if notify and peers:
        consumers = [peer for peer in peers if peer.role is notifications.Role.CONSUMER]
        producers = [peer for peer in peers if peer.role is notifications.Role.PRODUCER]
        delivery_options = {
            "timeout_seconds": settings.notify_timeout_seconds,
            "max_attempts": settings.notify_max_attempts,
            "backoff_seconds": settings.notify_retry_backoff_seconds,
        }
        deliveries = notifications.notify_peers(consumers, payload, **delivery_options)
        if all(delivery.ok for delivery in deliveries):
            deliveries.extend(
                notifications.notify_peers(producers, payload, **delivery_options)
            )
    return notifications.RolloverReport(tick=None, deliveries=deliveries)

def close_current_slot(*, notify: bool) -> notifications.RolloverReport:
    """Ask CarbonShift to flush and drain the slot clients have finished."""
    if not notify:
        return notifications.RolloverReport(tick=None)
    carbonshift_peer = next(peer for peer in peers if peer.name == "carbonshift")
    peer = notifications.Peer(
        name="carbonshift-close-slot",
        base_url=carbonshift_peer.base_url,
        path="/v1/admin/close-slot",
        order=10,
        ack_slot_field="current_slot",
    )
    deliveries = notifications.notify_peers(
        [peer],
        {"current_slot": clock.current_slot(), "slot_minutes": clock.slot_minutes},
        timeout_seconds=settings.notify_timeout_seconds,
        max_attempts=settings.notify_max_attempts,
        backoff_seconds=settings.notify_retry_backoff_seconds,
    )
    return notifications.RolloverReport(tick=None, deliveries=deliveries)

def rollover(*, notify: bool) -> notifications.RolloverReport:
    """Advance the clock one slot, observe the new slot, then notify peers.

    **Order matters, and it is not arbitrary.** The observation is taken
    *after* the clock moves and *before* the fan-out, which is the only
    sequence that mirrors reality:

    1. we enter slot N;
    2. we take the reading that belongs to slot N (a measurement can only be
       taken about the slot you are standing in);
    3. CarbonShift enters N and dispatches due jobs;
    4. the executor enters N and wakes its worker;
    5. clients submit work for N while the executor processes due jobs.

    Observing *before* advancing would report a reading for the slot we are
    leaving, which is a different number and would silently disable any
    correction the peers apply.
    """
    tick = clock.advance()
    current = tick.global_slot

    reading = source.observe(current)
    if reading is not None:
        observed_readings[current] = reading
    else:
        # Not an error: a remote source may simply not have settled this slot
        # yet. Logged at debug because it is the expected path for the stub.
        logger.debug("no reading available for slot %s", current)

    payload = notifications.build_rollover_payload(
        clock,
        source,
        horizon_slots=settings.forecast_horizon_slots,
        reading=reading,
    )
    deliveries = (
        notifications.notify_peers(
            peers,
            payload,
            timeout_seconds=settings.notify_timeout_seconds,
            max_attempts=settings.notify_max_attempts,
            backoff_seconds=settings.notify_retry_backoff_seconds,
        )
        if notify and peers
        else []
    )
    return notifications.RolloverReport(tick=tick, deliveries=deliveries)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness. Returns 503 when the active adapter is degraded (e.g. the
    remote upstream is unreachable), so an orchestrator sees it."""
    info = source.health()
    body = {
        "status": "ok" if info.get("ok") and not is_desynced() else "degraded",
        "source": info,
        "current_slot": clock.current_slot(),
        "manual_clock": clock.manual,
        # Manual emulation starts at today's midnight, so negative drift is
        # expected until the simulated clock catches up with wall time.
        "drift_slots": clock.drift_slots(),
        # Present only once a rollover has partially failed; see `_mark_desynced`.
        "desynced": _desynced,
    }
    healthy = info.get("ok") and not is_desynced()
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/v1/meta")
async def meta() -> MetaResponse:
    return MetaResponse(
        role=settings.role,
        source=source.name,
        slot_minutes=settings.slot_minutes,
        epoch_utc=clock.epoch.isoformat(),
        forecast_horizon_slots=settings.forecast_horizon_slots,
        manual_clock=clock.manual,
        # Realtime peers derive slot changes locally; the provider does not
        # run a periodic push/advance loop.
        auto_advance_clock=False,
        peers=[{"name": p.name, "url": p.url, "order": p.order} for p in peers],
    )


@app.get("/v1/slot")
async def current_slot() -> CurrentSlotResponse:
    now = clock.current_slot()
    return CurrentSlotResponse(
        current_slot=now,
        slot_start_utc=clock.slot_start(now).isoformat(),
        slot_minutes=settings.slot_minutes,
        manual_clock=clock.manual,
        local_step=clock.local_step(),
        source=source.name,
    )


@app.get("/v1/forecast")
async def forecast(
    from_slot: int | None = Query(default=None, description="Start slot; defaults to the current one."),
    count: int | None = Query(default=None, ge=1, le=288, description="How many slots; defaults to the configured horizon."),
) -> ForecastResponse:
    start = clock.current_slot() if from_slot is None else from_slot
    n = settings.forecast_horizon_slots if count is None else count
    points = source.forecast(start, n)
    return ForecastResponse(
        source=source.name,
        slot_minutes=settings.slot_minutes,
        current_slot=clock.current_slot(),
        points=[ForecastPointModel(**p.to_dict()) for p in points],
    )


@app.get("/v1/observed")
async def observed(slot: int | None = Query(default=None)) -> dict:
    """The reading taken for `slot`, if one was taken.

    `known: false` is the normal answer for any slot that has not been
    reached — a measurement of a future slot does not exist, so it is not
    reported as one.
    """
    current = clock.current_slot()
    target = current if slot is None else slot
    reading = observed_readings.get(target)
    if reading is None and not clock.manual and target == current:
        reading = source.observe(current)
        if reading is not None:
            observed_readings[current] = reading
    if reading is None:
        return {"slot": target, "known": False, "actual": None, "observed_at_slot": None}
    return {"known": True, **reading.to_dict()}


@app.post("/v1/advance-slot")
def advance_slot(body: AdvanceRequest | None = None) -> AdvanceResponse:
    """Drain the active slot, advance once, and start work for the next slot."""
    with _advance_lock:
        return _advance_slot_locked(body)


def _advance_slot_locked(body: AdvanceRequest | None) -> AdvanceResponse:
    """Manual-clock only: roll exactly one slot, then fan out.

    Client ticks for the active slot have already returned when this closes
    it. CarbonShift then flushes and drains that slot before the provider
    advances and notifies CarbonShift, the executor, and the clients of the
    next slot.
    """
    if not clock.manual:
        raise HTTPException(status_code=409, detail="PROVIDER_MANUAL_CLOCK is not enabled")

    # A desynced run must not be allowed to continue: the clock has moved but
    # at least one peer did not follow, so any further advance would compound
    # the divergence. Restart the stack to recover.
    if is_desynced():
        raise HTTPException(
            status_code=503,
            detail={"desynced": True, "reason": "a previous rollover failed; restart the stack", **_desynced},
        )


    request = body or AdvanceRequest()
    current = clock.current_slot()
    if request.expect_slot is not None and request.expect_slot != current:
        raise HTTPException(
            status_code=409,
            detail=f"slot mismatch: expected {request.expect_slot}, provider is at {current}",
        )

    announcement_deliveries: list[notifications.DeliveryResult] = []
    if clock.local_step() == 0:
        announcement = announce(notify=request.notify_peers)  # slot G
        if request.notify_peers and announcement.any_failed:
            raise HTTPException(
                status_code=503,
                detail={
                    "announcement_failed": True,
                    "all_ok": False,
                    "deliveries": [delivery.to_dict() for delivery in announcement.deliveries],
                },
            )
        announcement_deliveries = announcement.deliveries

    close_report = close_current_slot(notify=request.notify_peers)
    if request.notify_peers and close_report.any_failed:
        raise HTTPException(
            status_code=503,
            detail={
                "slot_close_failed": True,
                "current_slot": current,
                "all_ok": False,
                "deliveries": [delivery.to_dict() for delivery in close_report.deliveries],
            },
        )

    report = rollover(notify=request.notify_peers)  # slot G+1
    tick = report.tick
    complete_report = notifications.RolloverReport(
        tick=tick,
        deliveries=[
            *announcement_deliveries,
            *close_report.deliveries,
            *report.deliveries,
        ],
    )

    # Fail hard on fan-out failures. The provider clock has ALREADY moved at this point
    # and cannot be un-moved, so this is not "the advance didn't happen" — it
    # is "the clock moved and not every peer followed". Rolling time back would
    # be worse (some peers already advanced), so the honest response is to
    # refuse to continue and make the operator restart the stack.
    if request.notify_peers and report.any_failed:
        _mark_desynced(complete_report)
        raise HTTPException(
            status_code=503,
            detail={"desynced": True, **complete_report.to_dict()},
        )

    return AdvanceResponse(
        current_slot=tick.global_slot if tick else current,
        slot_start_utc=(tick.started_at_utc if tick else datetime.now(timezone.utc)).isoformat(),
        local_step=tick.local_step if tick else clock.local_step(),
        source=source.name,
        notified=request.notify_peers,
        all_ok=complete_report.all_ok,
        deliveries=[d.to_dict() for d in complete_report.deliveries],
    )
