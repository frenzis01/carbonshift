"""Tests for the provider clock — the single time master."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.clock import ProviderClock

EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)
START = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


def _manual(start: datetime = START) -> ProviderClock:
    return ProviderClock(manual=True, slot_minutes=30, epoch=EPOCH, start_at=start)


def _realtime() -> ProviderClock:
    return ProviderClock(manual=False, slot_minutes=30, epoch=EPOCH)


# ─── manual mode ─────────────────────────────────────────────────────────────


def test_manual_clock_is_frozen_until_advanced():
    clock = _manual()
    first = clock.now()
    assert clock.now() == first


def test_advance_moves_exactly_one_slot():
    clock = _manual()
    before = clock.current_slot()
    tick = clock.advance()
    assert tick.global_slot == before + 1
    assert tick.advanced_by == 1
    assert clock.current_slot() == before + 1


def test_repeated_advances_walk_one_slot_at_a_time():
    clock = _manual()
    slots = [clock.advance().global_slot for _ in range(4)]
    assert slots == sorted(slots)
    assert all(b - a == 1 for a, b in zip(slots, slots[1:]))


def test_local_step_counts_advances_and_is_monotonic():
    clock = _manual()
    assert clock.local_step() == 0
    clock.advance()
    clock.advance()
    assert clock.local_step() == 2


def test_advance_records_the_previous_slot_for_auditing():
    clock = _manual()
    before = clock.current_slot()
    tick = clock.advance()
    assert tick.previous_global_slot == before


def test_advance_requires_manual_mode():
    with pytest.raises(RuntimeError, match="manual clock"):
        _realtime().advance()


def test_manual_clock_floors_start_to_a_slot_boundary():
    # 12:17 must become 12:00, not stay 12:17.
    clock = _manual(start=datetime(2024, 5, 1, 12, 17, 45, tzinfo=timezone.utc))
    assert clock.now() == datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


def test_slot_start_is_the_inverse_of_the_current_slot():
    clock = _manual()
    clock.advance()
    slot = clock.current_slot()
    assert clock.slot_start(slot) == clock.now()


# ─── realtime mode ───────────────────────────────────────────────────────────


def test_realtime_clock_tracks_wall_time():
    clock = _realtime()
    assert abs((clock.now() - datetime.now(timezone.utc)).total_seconds()) < 5


def test_realtime_drift_is_reported_as_zero():
    assert _realtime().drift_slots() == 0


def test_manual_drift_tracks_the_simulated_instant():
    """`drift_slots` compares the frozen instant to real wall time, so its
    absolute sign depends on where the clock was started. The invariant that
    actually matters is that advancing moves it forward, one step per advance.
    """
    clock = _manual(start=START)
    before = clock.drift_slots()
    clock.advance()
    clock.advance()
    assert clock.drift_slots() == before + 2


def test_drift_is_monotonic_across_many_advances():
    clock = _manual(start=datetime.now(timezone.utc))
    previous = clock.drift_slots()
    for _ in range(5):
        clock.advance()
        current = clock.drift_slots()
        assert current >= previous
        previous = current


def test_realtime_clock_never_reports_drift():
    assert _realtime().drift_slots() == 0


def test_epoch_and_slot_minutes_are_exposed():
    clock = _manual()
    assert clock.slot_minutes == 30
    assert clock.epoch == EPOCH


def test_manual_flag_is_readable():
    assert _manual().manual is True
    assert _realtime().manual is False
