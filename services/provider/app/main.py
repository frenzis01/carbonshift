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
`POST /v1/advance-slot`   manual-clock only: roll one slot and fan out
`POST /v1/forecast`       not implemented (forecast is not caller-supplied)

The provider is intentionally stateless apart from the clock and the log of
readings it has taken: it keeps no per-request state, so it can be restarted at
any slot without losing anything a peer cannot re-derive from
`GET /v1/forecast`.
"""
from __future__ import annotations

import logging
import threading
import time
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
from .slots import parse_epoch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("provider.main")

# Build the source once at import: a bad PROVIDER_ROLE should fail at startup,
# not on the first slot rollover.
source = build_source(settings)
clock = ProviderClock(
    manual=settings.manual_clock,
    slot_minutes=settings.slot_minutes,
    epoch=parse_epoch(settings.epoch_iso),
)
peers = notifications.peers_from_settings(settings)

#: Readings actually taken, keyed by the slot they describe. Only ever
#: populated by `rollover`, i.e. only for slots that have been reached —
#: there is no way to obtain a reading for a future slot, by construction.
observed_readings: dict[int, ObservedPoint] = {}

_auto_advance_stop = threading.Event()
_auto_advance_thread: threading.Thread | None = None


def _auto_advance_loop() -> None:
    """Realtime role: roll the slot on our own every `slot_minutes`.

    Disabled when the clock is manual, because then the *client* drives the
    advance and two drivers would double-advance the system.
    """
    while not _auto_advance_stop.is_set():
        # Wake up a little before the boundary so the fan-out lands in the
        # new slot rather than racing its start.
        slept = _auto_advance_stop.wait(max(1.0, settings.slot_minutes * 60.0))
        if slept or _auto_advance_stop.is_set():
            continue
        try:
            rollover(notify=True)
        except Exception:  # never let the loop die on one bad rollover
            logger.exception("auto-advance rollover failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _auto_advance_thread
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
    if not clock.manual and settings.auto_advance_clock:
        _auto_advance_stop.clear()
        _auto_advance_thread = threading.Thread(
            target=_auto_advance_loop, daemon=True, name="provider-auto-advance"
        )
        _auto_advance_thread.start()
    yield
    _auto_advance_stop.set()


app = FastAPI(title="CarbonShift Carbon Intensity Provider", lifespan=lifespan)


def rollover(*, notify: bool) -> notifications.RolloverReport:
    """Advance the clock one slot, observe the new slot, then notify peers.

    **Order matters, and it is not arbitrary.** The observation is taken
    *after* the clock moves and *before* the fan-out, which is the only
    sequence that mirrors reality:

    1. we enter slot N;
    2. we take the reading that belongs to slot N (a measurement can only be
       taken about the slot you are standing in);
    3. we tell the peers "you are now in slot N, here is the reading, and here
       is the forecast for what comes next".

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
        "status": "ok" if info.get("ok") else "degraded",
        "source": info,
        "current_slot": clock.current_slot(),
        "manual_clock": clock.manual,
        # Non-zero is normal in a fast manual run (see clock.drift_slots).
        "drift_slots": clock.drift_slots(),
    }
    return JSONResponse(body, status_code=200 if info.get("ok") else 503)


@app.get("/v1/meta")
async def meta() -> MetaResponse:
    return MetaResponse(
        role=settings.role,
        source=source.name,
        slot_minutes=settings.slot_minutes,
        epoch_utc=clock.epoch.isoformat(),
        forecast_horizon_slots=settings.forecast_horizon_slots,
        manual_clock=clock.manual,
        auto_advance_clock=settings.auto_advance_clock and not clock.manual,
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
    target = clock.current_slot() if slot is None else slot
    reading = observed_readings.get(target)
    if reading is None:
        return {"slot": target, "known": False, "actual": None, "observed_at_slot": None}
    return {"known": True, **reading.to_dict()}


@app.post("/v1/advance-slot")
async def advance_slot(body: AdvanceRequest | None = None) -> AdvanceResponse:
    """Manual-clock only: roll exactly one slot, then fan out.

    409 if the clock is not manual (same convention as carbonshift and the
    executor). 409 also if `expect_slot` does not match the current slot,
    which is what makes a retried notification idempotent rather than
    double-advancing the system.
    """
    if not clock.manual:
        raise HTTPException(status_code=409, detail="PROVIDER_MANUAL_CLOCK is not enabled")

    request = body or AdvanceRequest()
    current = clock.current_slot()
    if request.expect_slot is not None and request.expect_slot != current:
        raise HTTPException(
            status_code=409,
            detail=f"slot mismatch: expected {request.expect_slot}, provider is at {current}",
        )

    # Fail hard on fan-out failures: if any peer delivery fails, raise an exception.
    if request.notify_peers and report.any_failed:
        raise HTTPException(
            status_code=503,
            detail={"desynced": True, **report.to_dict()},
        )

    report = rollover(notify=request.notify_peers)
    tick = report.tick
    return AdvanceResponse(
        current_slot=tick.global_slot if tick else current,
        slot_start_utc=(tick.started_at_utc if tick else datetime.now(timezone.utc)).isoformat(),
        local_step=tick.local_step if tick else clock.local_step(),
        source=source.name,
        notified=request.notify_peers,
        all_ok=report.all_ok,
        deliveries=[d.to_dict() for d in report.deliveries],
    )
