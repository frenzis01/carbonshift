"""Timeslot discretization helpers.

Deadlines/start times are absolute datetimes (with date, to avoid the
AM/PM-style ambiguity of a bare time-of-day); this module maps them onto
slot boundaries of a configurable duration, e.g. 19:32 with 30-minute slots
falls in the [19:30, 20:00) timeslot, so the effective deadline (the point
by which the request must be done) is 20:00 — the slot's *end*.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import settings


def _parse_epoch(epoch_iso: str) -> datetime:
    """Parse the configured epoch, tolerating the trailing `Z` the provider
    uses and normalising a naive instant to UTC (a naive epoch would make
    aware-minus-naive subtraction raise at runtime)."""
    try:
        epoch = datetime.fromisoformat(epoch_iso.replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch if epoch.tzinfo is not None else epoch.replace(tzinfo=timezone.utc)


#: Slot origin — MUST equal the provider's epoch, or the two services disagree
#: about where slot boundaries are (see `config.settings.provider_epoch_iso`).
_EPOCH = _parse_epoch(settings.provider_epoch_iso)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def floor_to_slot(dt: datetime, slot_minutes: float) -> datetime:
    """Start of the timeslot `dt` falls into (e.g. 19:32 -> 19:30)."""
    dt = _as_utc(dt)
    delta = timedelta(minutes=slot_minutes)
    slots_elapsed = (dt - _EPOCH) // delta
    return _EPOCH + slots_elapsed * delta


def slot_of(dt: datetime, slot_minutes: float) -> int:
    """The **global** slot index containing `dt` (0 = the epoch's slot).

    This is the same number space the provider publishes, so it is what lets
    the client translate "the system entered slot N" into "which of my
    requests belong to N" — see `state.get_plan_for_slot`.
    """
    dt = _as_utc(dt)
    return int((dt - _EPOCH) / timedelta(minutes=slot_minutes))


def ceil_to_slot_end(dt: datetime, slot_minutes: float) -> datetime:
    """End of the timeslot `dt` falls into (e.g. 19:32 -> 20:00); a `dt`
    that already sits exactly on a boundary maps to itself."""
    start = floor_to_slot(dt, slot_minutes)
    return dt if start == dt else start + timedelta(minutes=slot_minutes)


def slot_index(dt: datetime, slot_minutes: float, reference: datetime) -> int:
    """0-based index of the timeslot `dt` falls into, relative to the
    timeslot `reference` falls into."""
    ref_start = floor_to_slot(reference, slot_minutes)
    dt_start = floor_to_slot(dt, slot_minutes)
    return int((dt_start - ref_start) / timedelta(minutes=slot_minutes))
