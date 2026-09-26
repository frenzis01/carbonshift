"""Tests for the slot-batch submitter.

`submit` (to carbonshift) is monkeypatched, so no real carbonshift is needed.

Note what is *not* tested here any more: `run_plan`, `_run`, `_advance_slot`
and `_perturbed_actual_ci` are gone. The client no longer drives the clock or
invents carbon intensity — the provider does both (see
`provider/ARCHITECTURE.md` §2 and §10). What remains is one operation:
"submit the requests belonging to this slot".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import plan_runner
from app.tracker import RequestTracker


def make_fake_submit():
    calls = []
    counter = iter(range(1, 10_000))

    def fake_submit(deadline_seconds, callback_url, payload, task_id=None):
        rid = next(counter)
        calls.append({"deadline_seconds": deadline_seconds, "payload": payload, "task_id": task_id})
        return {"request_id": rid, "status": "scheduled", "scheduled_slot": 1,
                "eta_seconds": 1.0, "flavour": "Balanced", "carbon_cost": 1.0, "error": None}

    return fake_submit, calls


def _specs(reference: datetime, offsets_minutes: list[float], slot_minutes: float = 30.0):
    return [
        {
            "task": "text_generation",
            "input": {"prompt": str(i)},
            "start_at": reference + timedelta(minutes=off),
            "deadline_at": reference + timedelta(minutes=off) + timedelta(minutes=slot_minutes),
        }
        for i, off in enumerate(offsets_minutes)
    ]


# ─── grouping ────────────────────────────────────────────────────────────────


def test_group_by_slot_buckets_requests_by_start_at():
    reference = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    specs = _specs(reference, [0, 10, 35])
    groups = plan_runner._group_by_slot(specs, slot_minutes=30, reference=reference)
    assert set(groups.keys()) == {0, 1}
    assert len(groups[0]) == 2
    assert len(groups[1]) == 1


def test_group_by_slot_uses_the_passed_reference_not_min_start_at():
    """The reference is the plan's slot boundary, not `min(start_at)`.

    With jitter, `min(start_at)` is a mid-slot instant; measuring from it
    happens to work for positive jitter but is fragile. Passing the boundary
    explicitly makes the grouping correct by construction.
    """
    reference = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # First request starts 2 minutes *into* the slot.
    specs = _specs(reference, [2, 40])
    groups = plan_runner._group_by_slot(specs, slot_minutes=30, reference=reference)
    assert set(groups.keys()) == {0, 1}


def test_get_batch_from_slot_returns_only_that_slot():
    reference = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    specs = _specs(reference, [0, 5, 35])
    assert len(plan_runner.get_batch_from_slot(specs, 30.0, 0, reference)) == 2
    assert len(plan_runner.get_batch_from_slot(specs, 30.0, 1, reference)) == 1
    assert plan_runner.get_batch_from_slot(specs, 30.0, 9, reference) == []


# ─── submission ──────────────────────────────────────────────────────────────


def test_send_slot_batch_submits_every_request_and_tracks_them(monkeypatch, tmp_path):
    fake_submit, calls = make_fake_submit()
    monkeypatch.setattr(plan_runner, "submit", fake_submit)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    reference = datetime.now(timezone.utc)
    batch = _specs(reference, [0, 5])

    submitted = plan_runner.send_slot_batch(tracker, batch, slot_minutes=30.0)

    assert submitted == 2
    assert len(calls) == 2
    assert len(tracker.all()) == 2


def test_send_slot_batch_returns_zero_for_an_empty_batch(monkeypatch, tmp_path):
    fake_submit, calls = make_fake_submit()
    monkeypatch.setattr(plan_runner, "submit", fake_submit)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    assert plan_runner.send_slot_batch(tracker, [], slot_minutes=30.0) == 0
    assert calls == []


def test_send_slot_batch_skips_a_failed_submit_without_aborting_the_slot(monkeypatch, tmp_path):
    """One bad request must not lose the rest of the slot's work."""
    from app.carbonshift_client import CarbonshiftError

    calls = []

    def flaky_submit(deadline_seconds, callback_url, payload, task_id=None):
        calls.append(task_id)
        if len(calls) == 1:
            raise CarbonshiftError("boom")
        return {"request_id": 99, "status": "scheduled", "scheduled_slot": 1,
                "eta_seconds": 1.0, "flavour": "Balanced", "carbon_cost": 1.0, "error": None}

    monkeypatch.setattr(plan_runner, "submit", flaky_submit)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    reference = datetime.now(timezone.utc)
    submitted = plan_runner.send_slot_batch(tracker, _specs(reference, [0, 5]), slot_minutes=30.0)

    assert len(calls) == 2          # both were attempted
    assert submitted == 1           # only the successful one is counted
    assert len(tracker.all()) == 1  # and only it is tracked


def test_send_slot_batch_derives_a_positive_deadline(monkeypatch, tmp_path):
    """`deadline_seconds` is what carbonshift schedules against, so it must be
    positive even when the deadline instant is already in the past."""
    fake_submit, calls = make_fake_submit()
    monkeypatch.setattr(plan_runner, "submit", fake_submit)

    tracker = RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds=60)
    past = datetime.now(timezone.utc) - timedelta(hours=5)
    plan_runner.send_slot_batch(tracker, _specs(past, [0]), slot_minutes=30.0)

    assert calls[0]["deadline_seconds"] >= 1.0

