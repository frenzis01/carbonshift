"""Pure unit tests for VirtualClock (no queue, no HTTP)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.clock import VirtualClock


def test_manual_clock_advances_by_slot_minutes():
    clock = VirtualClock(manual=True, slot_minutes=30)
    t0 = clock.now()
    t1 = clock.advance_to_next_slot()
    assert t1 - t0 == timedelta(minutes=30)
    t2 = clock.advance_to_next_slot()
    assert t2 - t1 == timedelta(minutes=30)


def test_manual_clock_now_is_slot_aligned():
    clock = VirtualClock(manual=True, slot_minutes=30)
    now = clock.now()
    assert now.minute in (0, 30)
    assert now.second == 0 and now.microsecond == 0


def test_non_manual_clock_returns_real_time_and_rejects_advance():
    clock = VirtualClock(manual=False, slot_minutes=30)
    before = datetime.now(timezone.utc)
    assert abs((clock.now() - before).total_seconds()) < 1
    with pytest.raises(RuntimeError):
        clock.advance_to_next_slot()
