"""Tests for the manual-clock emulation protocol: build isolated
`JobQueue`/`VirtualClock` instances directly (not the module-level
singleton used by `app.main`), so these don't interact with the
real-time test suite in `test_api.py`.
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

from app.clock import VirtualClock
from app.queue_worker import DuplicateRequestIdError, JobQueue


def fake_run_task(task, flavour, task_input):
    return {"output": {"echo": task_input}, "model": "fake", "confidence": None,
            "quality_score": None, "execution_time_seconds": 0.0}


def make_slow_run_task(delay_seconds):
    def _slow_run_task(task, flavour, task_input):
        time.sleep(delay_seconds)
        return {"output": {"echo": task_input}, "model": "fake", "confidence": None,
                "quality_score": None, "execution_time_seconds": delay_seconds}
    return _slow_run_task


def make_manual_queue(monkeypatch, slot_minutes=30.0):
    monkeypatch.setattr("app.queue_worker.run_task", fake_run_task)
    clock = VirtualClock(manual=True, slot_minutes=slot_minutes)
    queue = JobQueue(clock)
    queue.start()
    return queue, clock


def _wait_for_status(queue, request_id, status, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.get(request_id).status == status:
            return True
        time.sleep(0.02)
    return False


def test_job_due_now_runs_without_advancing(monkeypatch):
    queue, _clock = make_manual_queue(monkeypatch)
    job = queue.submit("text_generation", "fast", {"prompt": "p"}, execute_at=None, callback_url=None)
    assert _wait_for_status(queue, job.request_id, "completed")
    queue.stop()


def test_repeated_dispatch_id_returns_the_queued_job_without_enqueuing_twice():
    clock = VirtualClock(manual=True, slot_minutes=30.0)
    queue = JobQueue(clock)
    dispatch = {
        "task": "question_answering",
        "flavour": "accurate",
        "task_input": {"question": "q", "context": "c"},
        "execute_at": clock.now(),
        "callback_url": "http://carbonshift/v1/callback/duplicate-id",
        "request_id": "duplicate-id",
        "context": {"scheduled_slot": 1, "carbon_cost": 2.0},
    }

    first = queue.submit(**dispatch)
    repeated = queue.submit(**dispatch)

    assert repeated is first
    assert queue.snapshot() == {first.execute_at.replace(microsecond=0).isoformat(): ["duplicate-id"]}


def test_reusing_dispatch_id_for_different_work_is_rejected():
    clock = VirtualClock(manual=True, slot_minutes=30.0)
    queue = JobQueue(clock)
    dispatch = {
        "task": "question_answering",
        "flavour": "accurate",
        "task_input": {"question": "q", "context": "c"},
        "execute_at": clock.now(),
        "callback_url": "http://carbonshift/v1/callback/duplicate-id",
        "request_id": "duplicate-id",
        "context": {"scheduled_slot": 1, "carbon_cost": 2.0},
    }
    queue.submit(**dispatch)

    with pytest.raises(DuplicateRequestIdError, match="different dispatch data"):
        queue.submit(**{**dispatch, "task_input": {"question": "other"}})


def test_duplicate_without_explicit_execute_time_is_still_idempotent_after_clock_moves():
    clock = VirtualClock(manual=True, slot_minutes=30.0)
    queue = JobQueue(clock)
    dispatch = {
        "task": "question_answering",
        "flavour": "accurate",
        "task_input": {"question": "q", "context": "c"},
        "execute_at": None,
        "callback_url": "http://carbonshift/v1/callback/duplicate-clock",
        "request_id": "duplicate-clock",
        "context": {"scheduled_slot": 1, "carbon_cost": 2.0},
    }

    first = queue.submit(**dispatch)
    clock.advance_to_next_slot()
    repeated = queue.submit(**dispatch)

    assert repeated is first
    assert len(queue.snapshot()) == 1
    assert sum(len(job_ids) for job_ids in queue.snapshot().values()) == 1


def test_future_slot_job_waits_for_advance(monkeypatch):
    queue, clock = make_manual_queue(monkeypatch)
    future = clock.now() + timedelta(minutes=30)
    job = queue.submit("ner", "fast", {"text": "hi"}, execute_at=future, callback_url=None)

    time.sleep(0.1)
    assert queue.get(job.request_id).status == "queued"

    queue.advance_slot()
    assert queue.get(job.request_id).status == "completed"
    queue.stop()


def test_advance_slot_only_runs_jobs_up_to_the_new_slot(monkeypatch):
    queue, clock = make_manual_queue(monkeypatch)
    now = clock.now()  # already slot-aligned (VirtualClock floors at construction)
    due_now = queue.submit("text_generation", "fast", {"prompt": "a"}, execute_at=now, callback_url=None)
    due_later = queue.submit("text_generation", "fast", {"prompt": "b"},
                              execute_at=now + timedelta(minutes=60), callback_url=None)

    queue.advance_slot()  # -> now+30min
    assert queue.get(due_now.request_id).status == "completed"
    assert queue.get(due_later.request_id).status == "queued"

    queue.advance_slot()  # -> now+60min
    assert _wait_for_status(queue, due_later.request_id, "completed")
    queue.stop()


def test_advance_slot_waits_for_in_flight_execution_not_just_the_queue(monkeypatch):
    """Regression test: a job can be dequeued (invisible to `_slots`, e.g.
    downloading its model) while still running when `advance_slot()` checks
    whether anything is "still due" — it must wait for that job to actually
    finish, not just for the queue dict to look empty (found via live Docker
    testing: a cold model download made `advance_slot()` return while the
    job was still mid-execution)."""
    monkeypatch.setattr("app.queue_worker.run_task", make_slow_run_task(0.3))
    clock = VirtualClock(manual=True, slot_minutes=30.0)
    queue = JobQueue(clock)
    queue.start()

    job = queue.submit("text_generation", "fast", {"prompt": "p"}, execute_at=None, callback_url=None)
    time.sleep(0.05)  # let the worker thread dequeue it (now "busy", not in _slots)
    assert queue.get(job.request_id).status == "running"

    result = queue.advance_slot()
    assert queue.get(job.request_id).status == "completed"
    assert result["queue"] == {}
    queue.stop()


def test_advance_slot_response_reports_current_time_and_queue(monkeypatch):
    queue, clock = make_manual_queue(monkeypatch)
    result = queue.advance_slot()
    assert "current_time" in result
    assert "queue" in result
    queue.stop()
