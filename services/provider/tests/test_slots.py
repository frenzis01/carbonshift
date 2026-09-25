"""Tests for the slot arithmetic shared by both roles.

These matter more than they look: if the provider and carbonshift disagree on
slot boundaries by even one slot, the scheduler plans against the wrong carbon
curve and every `carbon_saving_pct` is quietly wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.slots import floor_to_slot, parse_epoch, parse_iso, slot_of, slot_start_utc

EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_parse_iso_accepts_upstream_z_suffix():
    # The GB API uses a trailing Z; naive fromisoformat handling would break.
    parsed = parse_iso("2018-01-20T12:00Z")
    assert parsed == datetime(2018, 1, 20, 12, 0, tzinfo=timezone.utc)


def test_parse_epoch_normalises_naive_to_utc():
    # A naive epoch would make aware-naive subtraction raise at runtime.
    assert parse_epoch("2020-01-01T00:00:00").tzinfo is not None


def test_parse_epoch_falls_back_on_garbage():
    assert parse_epoch("not-a-date") == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_slot_of_is_a_pure_function_of_time():
    # The whole cross-component rendezvous relies on this property.
    t = EPOCH + timedelta(minutes=95)
    assert slot_of(t, 30, EPOCH) == 3


def test_slot_of_floors_within_the_slot():
    start = EPOCH + timedelta(minutes=90)
    assert slot_of(start, 30, EPOCH) == slot_of(start + timedelta(minutes=29), 30, EPOCH)


def test_slot_start_utc_inverts_slot_of():
    t = EPOCH + timedelta(minutes=95)
    slot = slot_of(t, 30, EPOCH)
    start = slot_start_utc(slot, 30, EPOCH)
    assert slot_of(start, 30, EPOCH) == slot
    assert start <= t < start + timedelta(minutes=30)


def test_floor_to_slot_aligns_to_the_epoch_not_the_hour():
    # 19:32 with a 30-min slot from a midnight epoch -> 19:30.
    t = datetime(2024, 5, 1, 19, 32, tzinfo=timezone.utc)
    epoch = datetime(2024, 5, 1, tzinfo=timezone.utc)
    assert floor_to_slot(t, 30, epoch) == datetime(2024, 5, 1, 19, 30, tzinfo=timezone.utc)


def test_slot_of_is_negative_before_the_epoch():
    # Must not blow up: a caller catching up can legitimately ask for earlier.
    assert slot_of(EPOCH - timedelta(minutes=30), 30, EPOCH) == -1
