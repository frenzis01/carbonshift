"""Virtual clock abstraction: wall-clock by default, or a frozen clock that
only advances via an explicit `advance_to_next_slot()` call — used for
deterministic "fake time" emulation tests (mirrors carbonshift's own
`Config::manual_clock` / `POST /v1/admin/advance-slot`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class VirtualClock:
    def __init__(self, manual: bool, slot_minutes: float):
        self.manual = manual
        self.slot_minutes = slot_minutes
        self._virtual_now = _floor_to_slot(datetime.now(timezone.utc), slot_minutes) if manual else None

    def now(self) -> datetime:
        if not self.manual:
            return datetime.now(timezone.utc)
        assert self._virtual_now is not None
        return self._virtual_now

    def advance_to_next_slot(self) -> datetime:
        if not self.manual:
            raise RuntimeError("advance_to_next_slot() requires EXECUTOR_MANUAL_CLOCK=1")
        assert self._virtual_now is not None
        self._virtual_now = _floor_to_slot(self._virtual_now, self.slot_minutes) + timedelta(minutes=self.slot_minutes)
        return self._virtual_now


def _floor_to_slot(dt: datetime, slot_minutes: float) -> datetime:
    delta = timedelta(minutes=slot_minutes)
    slots_elapsed = (dt - _EPOCH) // delta
    return _EPOCH + slots_elapsed * delta
