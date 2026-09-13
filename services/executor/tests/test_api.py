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

from app import queue_worker
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
    resp = client.post("/dispatch", json={
        "request_id": 42,
        "scheduled_slot": 7,
        "flavour": "Balanced",
        "carbon_cost": 1.23,
        "callback_url": "http://example.invalid/cb",
        "payload": {"task": "text_generation", "input": {"prompt": "hello"}},
    })
    assert resp.status_code == 202
    assert resp.json()["request_id"] == "42"
    status = _wait_for_completion(client, "42")
    assert status["status"] == "completed"
    assert status["context"] == {"scheduled_slot": 7, "carbon_cost": 1.23}


def test_dispatch_rejects_unknown_task(client):
    resp = client.post("/dispatch", json={
        "request_id": 2, "scheduled_slot": 0, "flavour": "fast",
        "carbon_cost": 0.0, "callback_url": "http://example.invalid/",
        "payload": {"task": "not_a_task", "input": {}},
    })
    assert resp.status_code == 422


def test_metrics_summary_reflects_completed_jobs(client):
    resp = client.post("/jobs", json={
        "task": "text_generation", "flavour": "balanced", "input": {"prompt": "p"},
    })
    request_id = resp.json()["request_id"]
    _wait_for_completion(client, request_id)

    summary = client.get("/metrics/summary").json()
    assert summary["total_jobs"] >= 1
    assert "text_generation/balanced" in summary["by_task_flavour"]
