"""HTTP-level tests: `carbonshift_client.submit` is monkeypatched (no real
carbonshift/executor needed) so these exercise routing, the background
batch-sender, callback handling, and metrics reporting only.
"""
from __future__ import annotations

import itertools
import time

import pytest
from fastapi.testclient import TestClient

from app import runner
from app.main import app

_counter = itertools.count(1)


def fake_submit(deadline_seconds, callback_url, payload, task_id=None):
    return {
        "request_id": next(_counter),
        "status": "scheduled",
        "scheduled_slot": 3,
        "eta_seconds": 5.0,
        "flavour": "Balanced",
        "carbon_cost": 1.2,
        "baseline_carbon_cost": 2.4,
        "scheduled_at": 1700000000.0,
        "error": None,
    }


@pytest.fixture(scope="module", autouse=True)
def _patch_submit():
    original = runner.submit
    runner.submit = fake_submit
    yield
    runner.submit = original


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _wait_for_count(client, n, timeout=2.0):
    deadline = time.monotonic() + timeout
    items = []
    while time.monotonic() < deadline:
        items = client.get("/requests").json()
        if len(items) >= n:
            return items
        time.sleep(0.02)
    return items


def test_health(client):
    assert client.get("/health").status_code == 200


def test_send_batch_tracks_requests(client):
    resp = client.post("/run/send-batch", json={"task": "text_generation", "count": 3})
    assert resp.status_code == 202
    body = resp.json()
    assert body["count"] == 3

    items = _wait_for_count(client, 3)
    assert len(items) >= 3
    assert all(i["carbonshift_status"] == "scheduled" for i in items)


def test_callback_completes_a_tracked_request(client):
    client.post("/run/send-batch", json={"task": "question_answering", "count": 1})
    items = _wait_for_count(client, 1)
    request_id = next(i["request_id"] for i in items if i["task"] == "question_answering")

    resp = client.post("/callback", json={
        "request_id": int(request_id),
        "success": True,
        "result": {"task": "question_answering", "flavour": "balanced", "model": "m",
                   "output": {"answer": "Paris"}, "confidence": 0.95, "quality_score": 1.0,
                   "execution_time_seconds": 0.2},
        "error": None,
    })
    assert resp.status_code == 200

    detail = client.get(f"/requests/{request_id}").json()
    assert detail["status"] == "completed"
    assert detail["late"] is False


def test_callback_for_unknown_request_id_is_ignored_gracefully(client):
    resp = client.post("/callback", json={"request_id": 999999, "success": True, "result": {}, "error": None})
    assert resp.status_code == 200  # carbonshift/executor don't need to care, just logged


def test_unknown_request_detail_returns_404(client):
    resp = client.get("/requests/does-not-exist")
    assert resp.status_code == 404


def test_metrics_summary_reflects_batches(client):
    summary = client.get("/metrics/summary").json()
    assert summary["total_requests"] >= 4
    assert "text_generation/Balanced" in summary["by_task_flavour"]
    assert "overall" in summary


def test_metrics_summary_scheduler_section_degrades_gracefully_when_carbonshift_unreachable(client, monkeypatch):
    import app.main as main_module

    def raise_unreachable(*args, **kwargs):
        from app.carbonshift_client import CarbonshiftError
        raise CarbonshiftError("cannot reach carbonshift")

    monkeypatch.setattr(main_module, "get_stats", raise_unreachable)
    monkeypatch.setattr(main_module, "get_task_config", raise_unreachable)

    scheduler = client.get("/metrics/summary").json()["scheduler"]
    assert scheduler["global_error_avg"] is None
    assert scheduler["tasks"]["text_generation"]["max_error_threshold"] is None


def test_metrics_summary_scheduler_section_surfaces_carbonshift_data(client, monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "get_stats", lambda: {"global_error_avg": 12.3, "global_error_count": 7})
    monkeypatch.setattr(main_module, "get_task_config", lambda task_id: {"max_error_threshold": 17.5})

    scheduler = client.get("/metrics/summary").json()["scheduler"]
    assert scheduler["global_error_avg"] == 12.3
    assert scheduler["global_error_count"] == 7
    assert scheduler["tasks"]["text_generation"]["max_error_threshold"] == 17.5


def test_metrics_progress_reflects_batches(client):
    progress = client.get("/metrics/progress").json()
    assert progress["requests_sent"] >= 4
    assert progress["requests_scheduled"] >= 4
    assert progress["requests_completed"] >= 1
    assert progress["avg_carbon_saving_pct"] == pytest.approx(50.0)
