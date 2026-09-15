"""Tests for the timeslot plan runner: `submit` (to carbonshift) and the
admin `/advance-slot` HTTP calls are monkeypatched, so no real
carbonshift/executor is needed.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app import plan_runner
from app.tracker import RequestTracker


class FakeResponse:
    def __init__(self, json_body):
        self._json = json_body

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def make_fake_submit():
    calls = []
    counter = iter(range(1, 10_000))

    def fake_submit(deadline_seconds, callback_url, payload, task_id=None):
        rid = next(counter)
        calls.append({"deadline_seconds": deadline_seconds, "payload": payload, "task_id": task_id})
        return {"request_id": rid, "status": "scheduled", "scheduled_slot": 1,
                "eta_seconds": 1.0, "flavour": "Balanced", "carbon_cost": 1.0, "error": None}

    return fake_submit, calls


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_group_by_slot_buckets_requests_by_start_at():
    reference = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    specs = [
        {"start_at": reference},
        {"start_at": reference + timedelta(minutes=10)},
        {"start_at": reference + timedelta(minutes=35)},
    ]
    groups = plan_runner._group_by_slot(specs, slot_minutes=30)
    assert set(groups.keys()) == {0, 1}
    assert len(groups[0]) == 2
    assert len(groups[1]) == 1


def test_run_plan_realtime_submits_all_requests(monkeypatch, tmp_path):
    fake_submit, calls = make_fake_submit()
    monkeypatch.setattr(plan_runner, "submit", fake_submit)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    now = datetime.now(timezone.utc)
    specs = [
        {"task": "text_generation", "input": {"prompt": "a"}, "start_at": now, "deadline_at": now + timedelta(minutes=30)},
        {"task": "text_generation", "input": {"prompt": "b"}, "start_at": now, "deadline_at": now + timedelta(minutes=30)},
    ]
    batch_id, slots = plan_runner.run_plan(tracker, specs, slot_minutes=30, mode="realtime", executor_url=None)
    assert slots == 1
    assert isinstance(batch_id, str)

    assert _wait_until(lambda: len(calls) == 2)
    assert len(tracker.all()) == 2


def test_run_plan_emulated_calls_advance_slot_between_groups(monkeypatch, tmp_path):
    fake_submit, calls = make_fake_submit()
    monkeypatch.setattr(plan_runner, "submit", fake_submit)
    monkeypatch.setattr(plan_runner, "get_carbon_forecast", lambda: [100.0] * 10)

    advance_calls = []

    def fake_post(url, params=None, timeout=None):
        advance_calls.append(url)
        return FakeResponse({"current_slot": 1})

    monkeypatch.setattr(plan_runner.requests, "post", fake_post)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    now = datetime.now(timezone.utc)
    specs = [
        {"task": "text_generation", "input": {"prompt": "a"}, "start_at": now, "deadline_at": now + timedelta(minutes=30)},
        {"task": "text_generation", "input": {"prompt": "b"}, "start_at": now + timedelta(minutes=30),
         "deadline_at": now + timedelta(minutes=60)},
    ]
    batch_id, slots = plan_runner.run_plan(
        tracker, specs, slot_minutes=30, mode="emulated", executor_url="http://executor.invalid"
    )
    assert slots == 2

    assert _wait_until(lambda: len(advance_calls) == 4)
    # 2 slots x (carbonshift advance, executor advance), strictly in order.
    assert advance_calls[0].endswith("/v1/admin/advance-slot")
    assert advance_calls[1] == "http://executor.invalid/admin/advance-slot"
    assert advance_calls[2].endswith("/v1/admin/advance-slot")
    assert advance_calls[3] == "http://executor.invalid/admin/advance-slot"
    assert len(calls) == 2
