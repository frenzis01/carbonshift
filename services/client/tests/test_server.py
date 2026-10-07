"""HTTP-level tests: `carbonshift_client.submit` is monkeypatched (no real
carbonshift/executor needed) so these exercise routing, the background
batch-sender, callback handling, and metrics reporting only.
"""
from __future__ import annotations

import itertools
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import runner
from app.main import app

_counter = itertools.count(1)
_submit_calls = []


def fake_submit(deadline_seconds, callback_url, payload, **kwargs):
    _submit_calls.append({
        "task_kind": kwargs.get("task_kind"),
        "qos_profile_id": kwargs.get("qos_profile_id"),
    })
    return {
        "request_id": next(_counter),
        "qos_profile_id": kwargs.get("qos_profile_id"),
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
    # This module tests request routing and tracking; profile bootstrap gets
    # its own focused tests and must not call a live Carbonshift here.
    with patch("app.main.register_configured_qos_profiles", return_value=[]):
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


def _wait_for_profile(client, profile_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    items = []
    while time.monotonic() < deadline:
        items = client.get("/requests").json()
        if any(item.get("qos_profile_id") == profile_id for item in items):
            return items
        time.sleep(0.02)
    return items


def test_health(client):
    assert client.get("/health").status_code == 200


def test_send_batch_tracks_requests(client):
    first_call = len(_submit_calls)
    resp = client.post("/run/send-batch", json={"task": "text_generation", "count": 3})
    assert resp.status_code == 202
    body = resp.json()
    assert body["count"] == 3

    items = _wait_for_count(client, 3)
    assert len(items) >= 3
    assert all(i["carbonshift_status"] == "scheduled" for i in items)
    assert len(_submit_calls[first_call:]) == 3
    assert all(call["task_kind"] == "text_generation" for call in _submit_calls[first_call:])


def test_send_batch_propagates_and_tracks_an_explicit_qos_profile(client):
    profile_id = "question-answering-calibrated-v1"
    first_call = len(_submit_calls)
    resp = client.post("/run/send-batch", json={
        "task": "question_answering",
        "qos_profile_id": profile_id,
        "count": 1,
    })
    assert resp.status_code == 202

    items = _wait_for_profile(client, profile_id)
    tracked = next(item for item in items if item.get("qos_profile_id") == profile_id)
    assert tracked["task"] == "question_answering"
    assert tracked["qos_profile_id"] == profile_id
    sent = _submit_calls[first_call:]
    assert len(sent) == 1
    assert sent[0] == {
        "task_kind": "question_answering",
        "qos_profile_id": profile_id,
    }


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
    monkeypatch.setattr(main_module, "get_qos_profile", raise_unreachable)

    scheduler = client.get("/metrics/summary").json()["scheduler"]
    assert scheduler["global_error_avg"] is None
    assert scheduler["legacy_task_id_usage"] is None


def test_metrics_summary_scheduler_section_surfaces_carbonshift_data(client, monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "get_stats", lambda: {
        "global_error_avg": 12.3,
        "global_error_count": 7,
        "legacy_task_id_usage": {
            "request_submissions": 3,
            "task_api_calls": 1,
            "monitoring_queries": 2,
        },
    })
    monkeypatch.setattr(main_module, "get_qos_profile", lambda profile_id: {
        "task_kind": "question_answering",
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 17.5,
        "error_window": {"past_slots": 12, "future_slots": 14, "past_decay_slots": 12},
        "cumulative_error": {"enabled": True, "hard": True},
    })

    scheduler = client.get("/metrics/summary").json()["scheduler"]
    assert scheduler["global_error_avg"] == 12.3
    assert scheduler["global_error_count"] == 7
    assert scheduler["legacy_task_id_usage"]["request_submissions"] == 3
    assert scheduler["profiles"]["question-answering-calibrated-v1"]["error_semantics"] == "word-overlap-f1-v1"


def test_metrics_progress_reflects_batches(client):
    progress = client.get("/metrics/progress").json()
    assert progress["requests_sent"] >= 4
    assert progress["requests_scheduled"] >= 4
    assert progress["requests_completed"] >= 1
    assert progress["avg_carbon_saving_pct"] == pytest.approx(50.0)
