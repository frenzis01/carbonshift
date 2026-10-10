"""API/queue-plumbing tests: `run_task` (the actual HF inference) is
monkeypatched out, so these run fast and need no model download / torch /
GPU — they only exercise routing, queueing, callbacks-are-attempted, and
metrics recording. Real model behaviour is exercised manually (see
README.md "Test manuale con modelli reali").
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, queue_worker
from app.metrics import metrics_store
from app.main import app


def fake_run_task(task, flavour, task_input):
    return {
        "output": {"echo": task_input},
        "model": f"fake-{task}-{flavour}",
        "confidence": 0.9,
        "quality_score": None,
        "execution_time_seconds": 0.01,
    }


@pytest.fixture(scope="module", autouse=True)
def _patch_inference():
    original = queue_worker.run_task
    queue_worker.run_task = fake_run_task
    yield
    queue_worker.run_task = original


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _wait_for_completion(client, request_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/jobs/{request_id}").json()
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(0.02)
    return status


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_models_lists_all_tasks(client):
    resp = client.get("/models")
    body = resp.json()
    assert set(body.keys()) == {"text_generation", "ner", "question_answering"}
    for flavours in body.values():
        assert set(flavours.keys()) == {"accurate", "balanced", "fast"}


def test_submit_and_poll_job_runs_immediately(client):
    resp = client.post("/jobs", json={
        "task": "question_answering",
        "flavour": "fast",
        "input": {"question": "q", "context": "c"},
    })
    assert resp.status_code == 202
    request_id = resp.json()["request_id"]

    status = _wait_for_completion(client, request_id)
    assert status["status"] == "completed"
    assert status["result"] == {"echo": {"question": "q", "context": "c"}}


def test_future_job_stays_queued_until_due(client):
    future = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    resp = client.post("/jobs", json={
        "task": "ner", "flavour": "fast", "input": {"text": "hi"},
        "execute_at": future,
    })
    request_id = resp.json()["request_id"]

    status = client.get(f"/jobs/{request_id}").json()
    assert status["status"] == "queued"
    queue = client.get("/queue").json()
    assert any(request_id in ids for ids in queue.values())


def test_unknown_job_returns_404(client):
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404


def test_dispatch_adapter_matches_carbonshift_payload(client):
    execute_at = datetime.now(timezone.utc).isoformat()
    resp = client.post("/dispatch", json={
        "request_id": 42,
        "scheduled_slot": 7,
        "execute_at": execute_at,
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "http://example.invalid/cb",
        "payload": {"task": "text_generation", "input": {"prompt": "hello"}},
    })
    assert resp.status_code == 202
    assert resp.json()["request_id"] == "42"
    assert datetime.fromisoformat(resp.json()["execute_at"]).utcoffset() == timedelta(0)
    status = _wait_for_completion(client, "42")
    assert status["status"] == "completed"
    assert status["context"] == {"scheduled_slot": 7, "carbon_cost": 1.23}


def test_dispatch_keeps_a_future_utc_job_queued(client):
    execute_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    response = client.post("/dispatch", json={
        "request_id": 987654320,
        "scheduled_slot": 7,
        "execute_at": execute_at.isoformat(),
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "",
        "payload": {"task": "text_generation", "input": {"prompt": "hello"}},
    })

    assert response.status_code == 202
    status = client.get("/jobs/987654320").json()
    assert status["status"] == "queued"
    assert datetime.fromisoformat(status["execute_at"]) == execute_at


def test_duplicate_dispatch_does_not_execute_or_record_the_job_twice(client):
    request_id = 987654321
    dispatch = {
        "request_id": request_id,
        "scheduled_slot": 0,
        "execute_at": datetime.now(timezone.utc).isoformat(),
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "",
        "payload": {"task": "question_answering", "input": {"question": "q", "context": "c"}},
    }
    first = client.post("/dispatch", json=dispatch)
    assert first.status_code == 202
    assert _wait_for_completion(client, str(request_id))["status"] == "completed"

    retry = client.post("/dispatch", json=dispatch)

    assert retry.status_code == 202
    assert retry.json()["request_id"] == str(request_id)
    job_records = [
        record
        for record in metrics_store.raw(limit=10_000)
        if record["request_id"] == str(request_id)
    ]
    assert len(job_records) == 1


def test_dispatch_rejects_same_request_id_with_different_payload(client):
    request_id = 987654322
    dispatch = {
        "request_id": request_id,
        "scheduled_slot": 0,
        "execute_at": datetime.now(timezone.utc).isoformat(),
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "",
        "payload": {"task": "question_answering", "input": {"question": "q", "context": "c"}},
    }
    first = client.post("/dispatch", json=dispatch)
    assert first.status_code == 202
    assert _wait_for_completion(client, str(request_id))["status"] == "completed"

    conflicting_retry = client.post(
        "/dispatch",
        json={
            **dispatch,
            "payload": {"task": "question_answering", "input": {"question": "different", "context": "c"}},
        },
    )

    assert conflicting_retry.status_code == 409


def test_dispatch_requires_timezone_aware_execute_at(client):
    response = client.post("/dispatch", json={
        "request_id": 987654323,
        "scheduled_slot": 1,
        "execute_at": "2030-01-01T00:00:00",
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "",
        "payload": {"task": "question_answering", "input": {"question": "q", "context": "c"}},
    })
    assert response.status_code == 422


def test_native_jobs_reject_naive_execute_at(client):
    response = client.post("/jobs", json={
        "task": "text_generation",
        "flavour": "fast",
        "input": {"prompt": "hello"},
        "execute_at": "2030-01-01T00:00:00",
    })
    assert response.status_code == 422


def test_executor_rejects_provider_slot_length_mismatch(client, monkeypatch):
    monkeypatch.setattr(main.settings, "manual_clock", True)
    monkeypatch.setattr(main.settings, "slot_minutes", 30.0)

    response = client.post("/admin/advance-slot", json={"slot_minutes": 60.0})

    assert response.status_code == 409
    assert "does not match" in response.json()["detail"]


def test_executor_announce_uses_provider_utc_slot_boundary(client, monkeypatch):
    from app.clock import VirtualClock
    from app.queue_worker import JobQueue

    queue = JobQueue(VirtualClock(manual=True, slot_minutes=30.0))
    monkeypatch.setattr(main, "job_queue", queue)
    monkeypatch.setattr(main.settings, "manual_clock", True)
    monkeypatch.setattr(main.settings, "slot_minutes", 30.0)

    response = client.post("/admin/advance-slot", json={
        "kind": "announce",
        "current_slot": 123,
        "slot_minutes": 30.0,
        "slot_start_utc": "2024-05-01T12:00:00Z",
    })

    assert response.status_code == 200
    assert response.json()["current_time"] == "2024-05-01T12:00:00+00:00"


def test_dispatch_rejects_unknown_task(client):
    resp = client.post("/dispatch", json={
        "request_id": 2, "scheduled_slot": 0,
        "execute_at": datetime.now(timezone.utc).isoformat(),
        "flavour": "fast",
        "carbon_cost": 0.0, "callback_url": "http://example.invalid/",
        "payload": {"task": "not_a_task", "input": {}},
    })
    assert resp.status_code == 422


def test_dispatch_requires_execute_at(client):
    response = client.post("/dispatch", json={
        "request_id": 987654324,
        "scheduled_slot": 1,
        "flavour": "fast",
        "carbon_cost": 0.0,
        "callback_url": "",
        "payload": {"task": "text_generation", "input": {"prompt": "hello"}},
    })
    assert response.status_code == 422


def test_metrics_summary_reflects_completed_jobs(client):
    resp = client.post("/jobs", json={
        "task": "text_generation", "flavour": "balanced", "input": {"prompt": "p"},
    })
    request_id = resp.json()["request_id"]
    _wait_for_completion(client, request_id)

    summary = client.get("/metrics/summary").json()
    assert summary["total_jobs"] >= 1
    assert "text_generation/balanced" in summary["by_task_flavour"]
