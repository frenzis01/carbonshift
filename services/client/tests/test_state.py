"""Tests for the plan registry — the tick→plan-index mapping.

This is where the client's slot arithmetic lives, so these tests are the
regression net for the "which slot are we in" question. The invariant under
test throughout: **the client stores no clock**. Its position is always
`tick.current_slot - plan_start_slot`, computed on demand.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import state
from app.timeslots import floor_to_slot, slot_of

SLOT_MINUTES = 60.0


@pytest.fixture(autouse=True)
def _clean_registry():
    state.reset()
    yield
    state.reset()


def _specs(reference: datetime, offsets_minutes: list[float], slot_minutes: float = SLOT_MINUTES):
    return [
        {
            "task": "text_generation",
            "input": {"prompt": str(i)},
            "start_at": (reference + timedelta(minutes=off)).isoformat(),
            "deadline_at": (reference + timedelta(minutes=off + slot_minutes)).isoformat(),
        }
        for i, off in enumerate(offsets_minutes)
    ]


def _boundary(dt: datetime) -> datetime:
    return floor_to_slot(dt, SLOT_MINUTES)


# ─── registration ────────────────────────────────────────────────────────────


def test_store_plan_returns_a_monotonic_id():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    a = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    b = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    assert b > a


def test_store_plan_rejects_an_empty_plan():
    with pytest.raises(ValueError, match="at least one request"):
        state.store_plan([], SLOT_MINUTES, "emulated", None)


def test_plan_start_slot_is_the_global_slot_of_the_first_request():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 60]), SLOT_MINUTES, "emulated", None)
    assert state.get_plan(plan_id)["plan_start_slot"] == slot_of(ref, SLOT_MINUTES)


def test_slot_count_spans_the_plan():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 60, 120]), SLOT_MINUTES, "emulated", None)
    assert state.get_plan(plan_id)["slot_count"] == 3


def test_single_slot_plan_has_slot_count_one():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 5, 10]), SLOT_MINUTES, "emulated", None)
    assert state.get_plan(plan_id)["slot_count"] == 1


def test_store_plan_deep_copies_so_later_mutation_cannot_corrupt_it():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    specs = _specs(ref, [0])
    plan_id = state.store_plan(specs, SLOT_MINUTES, "emulated", None)
    specs[0]["task"] = "MUTATED"
    assert state.get_requests_from_plan(plan_id)[0]["task"] == "text_generation"


# ─── the tick → plan-index mapping ───────────────────────────────────────────


def test_get_plan_for_slot_maps_a_global_slot_to_plan_index_zero():
    """The core translation: a global slot minus the plan's start slot."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 60]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]

    assert state.get_plan_for_slot(start) == [(plan_id, 0)]
    assert state.get_plan_for_slot(start + 1) == [(plan_id, 1)]


def test_get_plan_for_slot_is_empty_before_the_plan_starts():
    """The provider ticks on its own schedule, so this is normal, not an error."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]
    assert state.get_plan_for_slot(start - 1) == []


def test_get_plan_for_slot_is_empty_after_the_plan_ends():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 60]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]
    assert state.get_plan_for_slot(start + 2) == []


def test_get_plan_for_slot_handles_a_large_global_slot_number():
    """Global slots are ~59000 today; the mapping must not care about magnitude."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]
    assert start > 50_000
    assert state.get_plan_for_slot(start) == [(plan_id, 0)]


def test_two_plans_in_different_slots_are_both_reported():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    a = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    b = state.store_plan(_specs(ref, [60]), SLOT_MINUTES, "emulated", None)
    start_a = state.get_plan(a)["plan_start_slot"]

    assert state.get_plan_for_slot(start_a) == [(a, 0)]
    assert state.get_plan_for_slot(start_a + 1) == [(b, 0)]


# ─── idempotency ─────────────────────────────────────────────────────────────


def test_a_slot_is_not_processed_before_it_is_marked():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]
    assert state.is_slot_already_processed(plan_id, start) is False


def test_marking_a_slot_makes_it_processed():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]

    state.mark_slot_processed(plan_id, start)
    assert state.is_slot_already_processed(plan_id, start) is True


def test_an_earlier_slot_counts_as_processed_once_a_later_one_is():
    """A retried tick for an older slot must also be a no-op."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0, 60]), SLOT_MINUTES, "emulated", None)
    start = state.get_plan(plan_id)["plan_start_slot"]

    state.mark_slot_processed(plan_id, start + 1)
    assert state.is_slot_already_processed(plan_id, start) is True


def test_an_unknown_plan_is_never_reported_as_processed():
    assert state.is_slot_already_processed(999, 0) is False


def test_marking_an_unknown_plan_is_a_no_op():
    state.mark_slot_processed(999, 0)  # must not raise


# ─── lookups ─────────────────────────────────────────────────────────────────


def test_get_plan_returns_none_for_an_unknown_id():
    assert state.get_plan(999) is None


def test_get_requests_from_plan_returns_none_for_an_unknown_id():
    assert state.get_requests_from_plan(999) is None


def test_reset_clears_everything():
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = state.store_plan(_specs(ref, [0]), SLOT_MINUTES, "emulated", None)
    state.reset()
    assert state.get_plan(plan_id) is None
    assert state.plans == {}


# ─── the invariant ───────────────────────────────────────────────────────────


def test_state_module_exposes_no_clock():
    """Guards the design rule: the provider owns the clock, so the client's
    plan registry must not grow a slot counter of its own."""
    for forbidden in ("current_slot", "advance_slot", "get_current_slot", "force_set_current_slot"):
        assert not hasattr(state, forbidden), (
            f"state.{forbidden} reintroduces a client-side clock; the provider "
            f"is the single owner of 'which slot are we in'"
        )
