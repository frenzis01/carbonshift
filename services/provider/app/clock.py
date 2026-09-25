"""The provider's clock: the single source of "what time is it" for the whole
system (see ARCHITECTURE.md §"Chi possiede il tempo").

Two independent things are tracked, and conflating them is the main hazard:

* **global slot index** — derived from `(now - epoch) / slot_minutes`. This is
  a pure function of the instant, so every component computes the same answer
  from the same wall clock. This is what gets published to peers.
* **monotonic slot counter** — how many advances this process has issued.
  Used only for ordering/telemetry.

The clock is deliberately *not* allowed to jump by arbitrary amounts: an
advance moves exactly one slot boundary, or reports a conflict. A multi-slot
jump would leave the peers with slots they never processed (the failure mode
the user asked about: "things lost because someone was behind the clock").
If a peer genuinely needs to catch up, it pulls `/v1/forecast?from_slot=…`
rather than being pushed a gap.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .slots import floor_to_slot, slot_of, slot_start_utc


@dataclass(frozen=True)
class SlotTick:
    """Immutable snapshot of one slot boundary being crossed."""

    global_slot: int
    local_step: int
    started_at_utc: datetime
    previous_global_slot: int

    @property
    def advanced_by(self) -> int:
        return self.global_slot - self.previous_global_slot


class ProviderClock:
    """Wall-clock by default; frozen and explicitly-advanced in manual mode.

    The `manual` flag is *not* a test-only convenience bolted on: it is what
    makes a single authoritative time master possible in emulation, because
    exactly one process may be allowed to move time forward.
    """

    def __init__(
        self,
        *,
        manual: bool,
        slot_minutes: float,
        epoch: datetime,
        start_at: datetime | None = None,
    ) -> None:
        self._manual = manual
        self._slot_minutes = slot_minutes
        self._epoch = epoch
        self._lock = threading.Lock()
        self._local_step = 0

        now = start_at if start_at is not None else datetime.now(timezone.utc)
        self._frozen_now = floor_to_slot(now, slot_minutes, epoch)

    # ── reads ─────────────────────────────────────────────────────────────

    @property
    def manual(self) -> bool:
        return self._manual

    @property
    def slot_minutes(self) -> float:
        return self._slot_minutes

    @property
    def epoch(self) -> datetime:
        return self._epoch

    def now(self) -> datetime:
        """The current instant — frozen at a slot boundary when manual."""
        if not self._manual:
            return datetime.now(timezone.utc)
        with self._lock:
            return self._frozen_now

    def current_slot(self) -> int:
        return slot_of(self.now(), self._slot_minutes, self._epoch)

    def local_step(self) -> int:
        with self._lock:
            return self._local_step

    def slot_start(self, slot: int) -> datetime:
        return slot_start_utc(slot, self._slot_minutes, self._epoch)

    # ── writes ────────────────────────────────────────────────────────────

    def advance(self) -> SlotTick:
        """Move to the next slot boundary and return the tick.

        Raises `RuntimeError` when the clock is not manual — the caller (the
        HTTP handler) turns that into a 409, mirroring carbonshift's and the
        executor's own `/admin/advance-slot` behaviour.

        A single advance moves exactly one slot *from the frozen instant*, and
        `global_slot` follows real wall time. That means in a slow manual run
        the two can diverge: `global_slot` tracks the calendar, the frozen
        instant tracks the simulation. The divergence is intentional and is
        reported by `/health` (`drift_slots`) instead of being hidden.
        """
        if not self._manual:
            raise RuntimeError("advance() requires the manual clock (PROVIDER_MANUAL_CLOCK=1)")

        with self._lock:
            previous_global = slot_of(self._frozen_now, self._slot_minutes, self._epoch)
            self._frozen_now = self._frozen_now + timedelta(minutes=self._slot_minutes)
            self._local_step += 1
            tick = SlotTick(
                global_slot=slot_of(self._frozen_now, self._slot_minutes, self._epoch),
                local_step=self._local_step,
                started_at_utc=self._frozen_now,
                previous_global_slot=previous_global,
            )
            return tick

    def drift_slots(self) -> int:
        """How far the frozen instant has drifted from real wall time.

        Non-zero is normal in a fast manual run (the simulation is ahead of
        the calendar); it is only a problem if it is *negative*, which would
        mean the simulation fell behind the slots it already published.
        """
        if not self._manual:
            return 0
        with self._lock:
            frozen = self._frozen_now
        real = floor_to_slot(datetime.now(timezone.utc), self._slot_minutes, self._epoch)
        return round((frozen - real).total_seconds() / 60.0 / self._slot_minutes)
