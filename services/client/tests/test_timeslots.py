"""Pure unit tests for timeslot discretization helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.timeslots import ceil_to_slot_end, floor_to_slot, slot_index


def test_floor_to_slot_rounds_down_to_boundary():
    dt = datetime(2026, 8, 26, 19, 32, tzinfo=timezone.utc)
    assert floor_to_slot(dt, 30) == datetime(2026, 8, 26, 19, 30, tzinfo=timezone.utc)


def test_ceil_to_slot_end_rounds_up_to_next_boundary():
    dt = datetime(2026, 8, 26, 19, 32, tzinfo=timezone.utc)
    assert ceil_to_slot_end(dt, 30) == datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def test_ceil_to_slot_end_exact_boundary_stays_put():
    dt = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
    assert ceil_to_slot_end(dt, 30) == dt


def test_slot_index_relative_to_reference():
    ref = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc)
    assert slot_index(ref, 30, ref) == 0
    assert slot_index(ref + timedelta(minutes=30), 30, ref) == 1
    assert slot_index(ref + timedelta(minutes=61), 30, ref) == 2


def test_floor_to_slot_naive_datetime_treated_as_utc():
    naive = datetime(2026, 8, 26, 19, 32)
    aware = datetime(2026, 8, 26, 19, 32, tzinfo=timezone.utc)
    assert floor_to_slot(naive, 30) == floor_to_slot(aware, 30)
