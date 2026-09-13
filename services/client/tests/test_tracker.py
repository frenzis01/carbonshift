"""Pure unit tests for RequestTracker (no HTTP, no carbonshift)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app.tracker import RequestTracker, TrackedRequest


def make_tracker(tmp_path, timeout_seconds=1.0) -> RequestTracker:
    return RequestTracker(str(tmp_path / "metrics.jsonl"), timeout_seconds)


def make_ack(**overrides):
    ack = {"status": "scheduled", "scheduled_slot": 3, "eta_seconds": 10.0,
           "flavour": "Balanced", "carbon_cost": 1.5, "baseline_carbon_cost": 3.0,
           "scheduled_at": 1700000000.0}
    ack.update(overrides)
    return ack


def test_on_callback_marks_completed_and_computes_latency(tmp_path):
    tracker = make_tracker(tmp_path)
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, submitted_at, make_ack()))

    found = tracker.on_callback("1", True, {"output": {"generated_text": "hi"}}, None)
    assert found is True

    record = tracker.get("1").to_dict()
    assert record["status"] == "completed"
    assert record["end_to_end_seconds"] >= 2.0
    assert record["late"] is False


def test_on_callback_marks_late_when_past_deadline(tmp_path):
    tracker = make_tracker(tmp_path)
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    tracker.add(TrackedRequest("1", "text_generation", 1.0, submitted_at, make_ack()))

    tracker.on_callback("1", True, {}, None)
    record = tracker.get("1").to_dict()
    assert record["late"] is True


def test_on_callback_for_unknown_id_returns_false(tmp_path):
    tracker = make_tracker(tmp_path)
    assert tracker.on_callback("missing", True, {}, None) is False


def test_on_callback_refreshes_stale_pending_ack(tmp_path, monkeypatch):
    tracker = make_tracker(tmp_path)
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    pending_ack = make_ack(status="pending", flavour=None, carbon_cost=None, scheduled_slot=None)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, submitted_at, pending_ack))

    fresh_ack = make_ack(status="completed", flavour="Fast", carbon_cost=0.8)
    monkeypatch.setattr("app.tracker.get_status", lambda request_id: fresh_ack)

    tracker.on_callback("1", True, {}, None)
    record = tracker.get("1").to_dict()
    assert record["flavour"] == "Fast"
    assert record["carbon_cost"] == 0.8


def test_on_callback_does_not_refresh_already_scheduled_ack(tmp_path, monkeypatch):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc), make_ack()))

    def boom(request_id):
        raise AssertionError("get_status should not be called when ack is not pending")

    monkeypatch.setattr("app.tracker.get_status", boom)
    assert tracker.on_callback("1", True, {}, None) is True


def test_sweep_timeouts_marks_stale_requests(tmp_path):
    tracker = make_tracker(tmp_path, timeout_seconds=0.1)
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    tracker.add(TrackedRequest("1", "ner", 30.0, submitted_at, make_ack()))

    tracker.sweep_timeouts()
    record = tracker.get("1").to_dict()
    assert record["status"] == "timed_out"


def test_sweep_timeouts_ignores_already_completed(tmp_path):
    tracker = make_tracker(tmp_path, timeout_seconds=0.1)
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    tracker.add(TrackedRequest("1", "ner", 30.0, submitted_at, make_ack()))
    tracker.on_callback("1", True, {}, None)

    tracker.sweep_timeouts()
    assert tracker.get("1").to_dict()["status"] == "completed"


def test_summary_groups_by_task_and_flavour(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(flavour="Fast")))
    tracker.add(TrackedRequest("2", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(flavour="Fast")))
    tracker.on_callback("1", True, {"confidence": 0.8, "quality_score": 0.5}, None)
    tracker.on_callback("2", False, None, "boom")

    summary = tracker.summary()
    group = summary["by_task_flavour"]["text_generation/Fast"]
    assert group["count"] == 2
    assert group["completed"] == 1
    assert group["failed"] == 1
    assert group["confidence"]["avg"] == 0.8


def test_summary_overall_aggregates_across_task_flavour_groups(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(flavour="Fast", carbon_cost=1.0, baseline_carbon_cost=2.0)))
    tracker.add(TrackedRequest("2", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(flavour="Accurate", carbon_cost=3.0, baseline_carbon_cost=3.0)))
    tracker.on_callback("1", True, {}, None)
    tracker.on_callback("2", True, {}, None)

    overall = tracker.summary()["overall"]
    assert overall["count"] == 2
    assert overall["completed"] == 2
    assert overall["carbon_cost"]["avg"] == 2.0  # (1.0 + 3.0) / 2, across both flavours


def test_to_dict_computes_carbon_saving_pct(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(carbon_cost=1.5, baseline_carbon_cost=3.0)))

    record = tracker.get("1").to_dict()
    assert record["baseline_carbon_cost"] == 3.0
    assert record["carbon_saving_pct"] == 50.0


def test_to_dict_converts_scheduled_at_to_iso8601(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(scheduled_at=1700000000.0)))

    record = tracker.get("1").to_dict()
    assert record["scheduled_at"] == datetime.fromtimestamp(1700000000.0, tz=timezone.utc).isoformat()


def test_to_dict_scheduled_at_is_none_when_still_pending(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(status="pending", scheduled_slot=None, scheduled_at=None)))

    record = tracker.get("1").to_dict()
    assert record["scheduled_at"] is None


def test_on_callback_exposes_execution_time_from_result(tmp_path):
    tracker = make_tracker(tmp_path)
    submitted_at = datetime.now(timezone.utc)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, submitted_at, make_ack()))

    tracker.on_callback("1", True, {"execution_time_seconds": 2.5}, None)
    record = tracker.get("1").to_dict()
    assert record["execution_time_seconds"] == 2.5


def test_to_dict_computes_energy_saving_pct_from_baseline_execution_time(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc), make_ack()))

    tracker.on_callback("1", True, {"execution_time_seconds": 1.0, "baseline_execution_time_seconds": 4.0}, None)
    record = tracker.get("1").to_dict()
    assert record["baseline_execution_time_seconds"] == 4.0
    assert record["energy_saving_pct"] == 75.0


def test_summary_includes_execution_time_and_carbon_saving_stats(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(flavour="Fast", carbon_cost=1.5, baseline_carbon_cost=3.0)))
    tracker.on_callback("1", True, {"execution_time_seconds": 2.0, "baseline_execution_time_seconds": 4.0}, None)

    summary = tracker.summary()
    group = summary["by_task_flavour"]["text_generation/Fast"]
    assert group["execution_time_seconds"]["avg"] == 2.0
    assert group["baseline_execution_time_seconds"]["avg"] == 4.0
    assert group["energy_saving_pct"]["avg"] == 50.0
    assert group["baseline_carbon_cost"]["avg"] == 3.0
    assert group["carbon_saving_pct"]["avg"] == 50.0


def test_progress_reports_counts_and_averages(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add(TrackedRequest("1", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(carbon_cost=1.5, baseline_carbon_cost=3.0)))
    tracker.add(TrackedRequest("2", "text_generation", 30.0, datetime.now(timezone.utc),
                                make_ack(carbon_cost=1.5, baseline_carbon_cost=3.0)))
    tracker.on_callback("1", True, {"confidence": 0.9, "execution_time_seconds": 1.0,
                                    "baseline_execution_time_seconds": 2.0}, None)
    tracker.on_callback("2", False, None, "boom")

    progress = tracker.progress()
    assert progress["requests_sent"] == 2
    assert progress["requests_scheduled"] == 2
    assert progress["requests_completed"] == 1
    assert progress["requests_failed"] == 1
    assert progress["avg_confidence"] == 0.9
    assert progress["avg_execution_time_seconds"] == 1.0
    assert progress["avg_carbon_saving_pct"] == 50.0
    assert progress["avg_energy_saving_pct"] == 50.0
