"""Slot arithmetic shared by both provider roles and by the notification glue.

Two different "slot" identities must never be mixed up (see ARCHITECTURE.md):

- **global slot** — a pure function of wall-clock time:
  ``global_slot(t) = floor((t - epoch) / slot_duration)``.
  This is what makes the provider's series directly comparable with any other
  component's, because it does not depend on when a given process started.
- **local index** — a position inside one forecast array (0 = the slot the
  caller asked about).

CarbonShift's `current_slot` is a local index into its finite planning
horizon, not a global slot number. Manual mode maps provider ticks into that
index; realtime mode starts the local index at the same wall-clock boundary.
The provider-to-engine offset remains explicit at the HTTP boundary.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

_EPOCH_FALLBACK = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 instant, tolerating the trailing ``Z`` the upstream
    GB API uses (Python's `fromisoformat` only gained ``Z`` support in 3.11,
    and even there it is worth normalising explicitly)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_epoch(epoch_iso: str) -> datetime:
    """Parse the configured epoch, normalising a naive instant to UTC.

    A naive epoch is a real hazard: subtracting a naive from an aware
    datetime raises `TypeError`, which would only surface at runtime.
    """
    try:
        epoch = parse_iso(epoch_iso)
    except ValueError:
        return _EPOCH_FALLBACK
    return epoch if epoch.tzinfo is not None else epoch.replace(tzinfo=timezone.utc)


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def local_midnight_utc(now_local: datetime | None = None) -> datetime:
    """Return today's local midnight as a UTC instant.

    Manual emulation starts at midnight for a readable day-based slot axis.
    The timestamp is still converted to UTC before it is shared with peers.
    `now_local` is injectable so timezone conversion can be tested without
    changing the host's timezone.
    """
    if now_local is None:
        local_date = datetime.now().astimezone().date()
        return datetime.combine(local_date, time.min).astimezone(timezone.utc)
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    midnight = datetime.combine(now_local.date(), time.min, tzinfo=now_local.tzinfo)
    return midnight.astimezone(timezone.utc)


def floor_to_slot(dt: datetime, slot_minutes: float, epoch: datetime) -> datetime:
    """Start of the slot `dt` falls into, aligned to `epoch`.

    Mirrors `client/app/timeslots.py::floor_to_slot` but with an explicit
    epoch parameter, because the two services must agree on slot *boundaries*
    for the series to line up.
    """
    dt = as_utc(dt)
    epoch = as_utc(epoch)
    delta = timedelta(minutes=slot_minutes)
    slots_elapsed = (dt - epoch) // delta
    return epoch + slots_elapsed * delta


def slot_of(dt: datetime, slot_minutes: float, epoch: datetime) -> int:
    """The **global slot index** containing `dt` (0 = the epoch's slot)."""
    dt = as_utc(dt)
    epoch = as_utc(epoch)
    return int((dt - epoch) / timedelta(minutes=slot_minutes))


def slot_start_utc(slot: int, slot_minutes: float, epoch: datetime) -> datetime:
    """Inverse of `slot_of`: the UTC instant a global slot begins."""
    return as_utc(epoch) + timedelta(minutes=slot_minutes) * slot
