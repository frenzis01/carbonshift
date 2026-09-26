"""Ordered, retrying fan-out of one slot rollover to the peer services.

This is where the user's "is there an ordering problem?" question is answered
concretely. The design is: **push an idempotent slot notification, in a fixed
order, with bounded retries — and make the clock advance strictly after the
peers have acknowledged.**

Why push rather than have each peer poll:
* the provider is the only component that knows a new slot has *started*; if
  peers polled, each would need its own timer and they could disagree about
  which slot is current during the window between their timers firing;
* in manual-clock emulation, a poller would have to be *told* to advance
  anyway, which amounts to the same push with more moving parts.

Why a fixed order (carbonshift before executor):
* carbonshift owns *dispatch*. If the executor advanced first, it could
  receive `/dispatch` calls for a slot it has already moved past — a job that
  silently misses its slot. Advancing carbonshift first guarantees that
  everything due for the slot just left has been handed off before the
  executor's own clock moves.
* the order is a *policy*, declared in one place, not implied by dict order.

Why retries + a report rather than "fire and forget":
* the current client-side launcher ignores advance failures entirely
  (`_advance_slot` logs and `return`s), so a peer that is briefly down
  silently desynchronizes the run. Here every attempt is recorded and the
  caller can refuse to advance, or surface a degraded state, instead of
  pretending success.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from enum import Enum

import requests

from .clock import ProviderClock, SlotTick
from .source import CarbonIntensitySource, ObservedPoint

logger = logging.getLogger("provider.notifications")


class Role(str, Enum):
    """What a peer does with a slot rollover.

    This is not decoration: it is the *reason* the fan-out order is what it is
    (see the module docstring). A PRODUCER supplies the work for the slot that
    is starting; a CONSUMER processes it. Producers must be notified first, or
    their work arrives after the slot it belongs to has already been handled.
    """

    CONSUMER = "consumer"
    PRODUCER = "producer"


@dataclass
class Peer:
    """A component to notify on each slot rollover.

    `advance_path` is per-peer rather than hard-coded so the same fan-out
    works against the client (`/v1/tick`), carbonshift
    (`/v1/admin/advance-slot`) and the executor (`/admin/advance-slot`), and
    can be retargeted without touching logic.
    """

    name: str
    base_url: str
    advance_path: str
    order: int
    # Defaulted to CONSUMER because that is the common case; producers opt in
    # explicitly at the one place that builds the list.
    role: Role = Role.CONSUMER

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.advance_path}"


@dataclass
class DeliveryResult:
    peer: str
    url: str
    ok: bool
    attempts: int = 0
    status_code: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "peer": self.peer,
            "url": self.url,
            "ok": self.ok,
            "attempts": self.attempts,
            "status_code": self.status_code,
            "error": self.error,
        }


@dataclass
class RolloverReport:
    """Outcome of one rollover fan-out."""

    tick: Optional[SlotTick]
    deliveries: list[DeliveryResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.deliveries) and all(d.ok for d in self.deliveries)

    @property
    def any_failed(self) -> bool:
        return any(not d.ok for d in self.deliveries)

    def to_dict(self) -> dict:
        return {
            "slot": self.tick.global_slot if self.tick else None,
            "local_step": self.tick.local_step if self.tick else None,
            "all_ok": self.all_ok,
            "deliveries": [d.to_dict() for d in self.deliveries],
        }


def build_rollover_payload(
    clock: ProviderClock,
    source: CarbonIntensitySource,
    *,
    horizon_slots: int,
    reading: Optional[ObservedPoint] = None,
) -> dict[str, Any]:
    """The body pushed to every peer.

    Two distinct things are carried, and keeping them separate is deliberate:

    * `forecast` — the *whole* window, because it is a prediction and spans
      many slots. Sending the whole window (not just the current value) means
      a peer that missed a notification can recover from the next one without
      a separate pull protocol.
    * `observed` — the single reading taken for the slot being entered, or
      `null`. A measurement concerns exactly one slot and only exists once
      that slot has been reached, so it is never a list and never covers the
      future.

    Earlier revisions folded these into one list of points each carrying an
    optional `actual`, which made "the actual of a future slot" expressible.
    It is not a thing that exists, so the shape no longer allows it.

    `observed` is `null` when no reading could be taken — the honest answer,
    rather than substituting the forecast for a measurement.
    """
    current = clock.current_slot()
    points = source.forecast(current, horizon_slots)

    return {
        "source": source.name,
        "current_slot": current,
        "slot_start_utc": clock.slot_start(current).isoformat(),
        "observed": reading.to_dict() if reading is not None else None,
        "forecast": [{"slot": p.slot, "forecast": p.forecast} for p in points],
    }


def notify_peers(
    peers: list[Peer],
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    max_attempts: int,
    backoff_seconds: float,
    post: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> list[DeliveryResult]:
    """POST `payload` to every peer, in `order`, with bounded retries.

    Stops at the first peer that fails all its attempts: continuing to
    advance later peers while an earlier one is behind is exactly the
    "someone is behind the clock" hazard, so a partial failure is reported
    rather than papered over.

    `post`/`sleep` are resolved *at call time* (not bound as defaults) so that
    monkeypatching `requests.post` in a test actually takes effect — a default
    argument would capture the original function object permanently.
    """
    if post is None:
        post = requests.post
    if sleep is None:
        import time

        sleep = time.sleep

    results: list[DeliveryResult] = []
    for peer in sorted(peers, key=lambda p: p.order):
        result = _deliver(peer, payload, timeout_seconds, max_attempts, backoff_seconds, post, sleep)
        results.append(result)
        if not result.ok:
            logger.warning(
                "rollover fan-out aborted at peer=%s after %d attempt(s): %s",
                peer.name,
                result.attempts,
                result.error,
            )
            break
    return results


def _deliver(
    peer: Peer,
    payload: dict[str, Any],
    timeout_seconds: float,
    max_attempts: int,
    backoff_seconds: float,
    post: Callable[..., Any],
    sleep: Callable[[float], None],
) -> DeliveryResult:
    last_error: Optional[str] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            resp = post(peer.url, json=payload, timeout=timeout_seconds)
            status = getattr(resp, "status_code", None)
            # 409 is the *expected* answer from a peer whose own manual clock
            # is disabled; treat it as a hard, non-retryable configuration
            # error rather than retrying it pointlessly.
            if status == 409:
                return DeliveryResult(
                    peer=peer.name,
                    url=peer.url,
                    ok=False,
                    attempts=attempt,
                    status_code=status,
                    error="peer refused: its manual clock is not enabled (409)",
                )
            if status is not None and 200 <= status < 300:
                return DeliveryResult(
                    peer=peer.name, url=peer.url, ok=True, attempts=attempt, status_code=status
                )
            last_error = f"unexpected status {status}"
        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < max_attempts:
            sleep(backoff_seconds * attempt)

    return DeliveryResult(
        peer=peer.name, url=peer.url, ok=False, attempts=max_attempts, error=last_error
    )


def peers_from_settings(settings) -> list[Peer]:
    """Build the peer list (and its policy order) from configuration.

    Order rationale is documented in the module docstring; keeping it here
    makes it a single reviewable place.
    """
    peers = [
        Peer(
            name="client",
            base_url=settings.client_url,
            advance_path="/v1/tick",
            order=10,
            role=Role.PRODUCER
        ),
        Peer(
            name="carbonshift",
            base_url=settings.carbonshift_url,
            # carbonshift already exposes exactly this endpoint; the provider
            # assumes the *role* of the clock driver the client plays today.
            advance_path="/v1/admin/advance-slot",
            order=20,
            role=Role.CONSUMER,
        )
    ]
    if settings.executor_url.strip():
        peers.append(
            Peer(
                name="executor",
                base_url=settings.executor_url,
                advance_path="/admin/advance-slot",
                order=30,
                role=Role.CONSUMER,
            )
        )
    return peers
