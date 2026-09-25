"""Timeslot discretization helpers.

Deadlines/start times are absolute datetimes (with date, to avoid the
AM/PM-style ambiguity of a bare time-of-day); this module maps them onto
slot boundaries of a configurable duration, e.g. 19:32 with 30-minute slots
falls in the [19:30, 20:00) timeslot, so the effective deadline (the point
by which the request must be done) is 20:00 — the slot's *end*.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# take EPOCH from settings
from .config import settings
_EPOCH = datetime.fromtimestamp(settings.provider_epoch, tz=timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def floor_to_slot(dt: datetime, slot_minutes: float) -> datetime:
    """Start of the timeslot `dt` falls into (e.g. 19:32 -> 19:30)."""
    dt = _as_utc(dt)
    delta = timedelta(minutes=slot_minutes)
    slots_elapsed = (dt - _EPOCH) // delta
    return _EPOCH + slots_elapsed * delta


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
