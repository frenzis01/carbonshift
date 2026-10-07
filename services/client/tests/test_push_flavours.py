"""Calibration rows become stable profile definitions with millisecond costs."""
from __future__ import annotations

from app.qos_profiles import build_task_profiles, default_error_threshold


def test_profile_builder_groups_latest_task_flavours_and_converts_units():
    stats = {
        "distilgpt2": {"task": "text_generation", "flavour": "fast",
                       "error_pct": 5.1, "avg_execution_time_seconds": 0.02},
        "gpt2": {"task": "text_generation", "flavour": "balanced",
                 "error_pct": 2.3, "avg_execution_time_seconds": 0.05},
        "distilbert-base-cased-distilled-squad": {"task": "question_answering", "flavour": "fast",
                                                    "error_pct": 1.0, "avg_execution_time_seconds": 0.016},
    }
    result = build_task_profiles(stats)
    tg = {f["name"]: f for f in result["text_generation"]["flavours"]}
    assert tg["Fast"]["error"] == 5.1
    assert tg["Fast"]["duration"] == 20  # 0.02s -> 20ms
    assert tg["Balanced"]["duration"] == 50

    assert len(result["question_answering"]["flavours"]) == 1


def test_duration_is_never_zero_for_very_fast_models():
    stats = {"m": {"task": "ner", "flavour": "fast", "error_pct": 0.0, "avg_execution_time_seconds": 0.0001}}
    result = build_task_profiles(stats)
    assert result["ner"]["flavours"][0]["duration"] == 1


def test_stale_model_for_the_same_task_flavour_is_superseded_by_the_newer_one():
    # e.g. the configured QA "balanced" model was swapped after an earlier
    # calibration run — both entries linger in model_stats.json (keyed by
    # model id), but only the newer one should ever be pushed.
    stats = {
        "old-model": {"task": "question_answering", "flavour": "balanced",
                      "error_pct": 38.9, "avg_execution_time_seconds": 0.17,
                      "measured_at": "2026-09-07T14:30:00+00:00"},
        "new-model": {"task": "question_answering", "flavour": "balanced",
                      "error_pct": 13.5, "avg_execution_time_seconds": 0.10,
                      "measured_at": "2026-09-07T16:00:00+00:00"},
    }
    result = build_task_profiles(stats)
    assert len(result["question_answering"]["flavours"]) == 1
    assert result["question_answering"]["flavours"][0]["error"] == 13.5


def test_default_error_threshold_is_75_percent_between_min_and_max():
    flavours = [{"name": "Accurate", "error": 10.0}, {"name": "Fast", "error": 20.0}]
    assert default_error_threshold(flavours, 0.75) == 17.5


def test_default_error_threshold_position_zero_is_the_minimum():
    flavours = [{"name": "Accurate", "error": 10.0}, {"name": "Fast", "error": 20.0}]
    assert default_error_threshold(flavours, 0.0) == 10.0


def test_profile_builder_creates_a_stable_complete_profile_for_legacy_calibration_data():
    stats = {
        "qa-model": {
            "task": "question_answering",
            "flavour": "accurate",
            "error_pct": 3.0,
            "avg_execution_time_seconds": 0.2,
        },
        "qa-fast": {
            "task": "question_answering",
            "flavour": "fast",
            "error_pct": 15.0,
            "avg_execution_time_seconds": 0.05,
        },
    }

    profile = build_task_profiles(stats)["question_answering"]

    assert profile["profile_id"] == "question_answering-calibrated-v1"
    assert profile["task_kind"] == "question_answering"
    assert profile["error_semantics"] == "word-overlap-f1-v1"
    assert profile["max_error_threshold"] == 12.0
    assert profile["error_window"] == {
        "past_slots": 12,
        "future_slots": 14,
        "past_decay_slots": 12,
    }
    assert profile["cumulative_error"] == {"enabled": True, "hard": True}
    assert all(flavour["duration"] >= 1 for flavour in profile["flavours"])


def test_profile_builder_uses_recorded_semantics_for_open_task_kinds():
    stats = {
        "model": {
            "task": "summarization",
            "flavour": "balanced",
            "error_pct": 12.0,
            "avg_execution_time_seconds": 0.08,
            "error_semantics": "rouge-l-error-v1",
        }
    }

    profile = build_task_profiles(stats)["summarization"]

    assert profile["profile_id"] == "summarization-calibrated-v1"
    assert profile["task_kind"] == "summarization"
    assert profile["error_semantics"] == "rouge-l-error-v1"


def test_profile_builder_allows_an_explicit_version_bump():
    stats = {
        "qa-model": {
            "task": "question_answering",
            "flavour": "balanced",
            "error_pct": 5.0,
            "avg_execution_time_seconds": 0.1,
            "error_semantics": "word-overlap-f1-v1",
        }
    }

    profile = build_task_profiles(stats, profile_version="v2")["question_answering"]

    assert profile["profile_id"] == "question_answering-calibrated-v2"



def test_profile_builder_rejects_mixed_semantics_for_one_task():
    stats = {
        "accurate": {
            "task": "custom_task",
            "flavour": "accurate",
            "error_pct": 1.0,
            "avg_execution_time_seconds": 0.1,
            "error_semantics": "metric-a-v1",
        },
        "fast": {
            "task": "custom_task",
            "flavour": "fast",
            "error_pct": 5.0,
            "avg_execution_time_seconds": 0.02,
            "error_semantics": "metric-b-v1",
        },
    }

    try:
        build_task_profiles(stats)
    except ValueError as exc:
        assert "inconsistent calibrated error semantics" in str(exc)
    else:
        raise AssertionError("profiles must not pool calibration rows with different metrics")


def test_threshold_position_must_be_within_the_interpolation_range():
    flavours = [{"name": "Accurate", "error": 10.0}, {"name": "Fast", "error": 20.0}]

    try:
        default_error_threshold(flavours, 1.1)
    except ValueError as exc:
        assert "between 0 and 1" in str(exc)
    else:
        raise AssertionError("invalid threshold positions must not create out-of-range policies")
