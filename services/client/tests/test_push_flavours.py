"""`build_task_flavours` groups `model_stats.json` entries by task and
converts real (fractional-second) measurements into carbonshift's
integer `duration` field (milliseconds — see push_flavours.py docstring)."""
from __future__ import annotations

from scripts.push_flavours import build_task_flavours, default_error_threshold


def test_groups_entries_by_task_and_converts_units():
    stats = {
        "distilgpt2": {"task": "text_generation", "flavour": "fast",
                       "error_pct": 5.1, "avg_execution_time_seconds": 0.02},
        "gpt2": {"task": "text_generation", "flavour": "balanced",
                 "error_pct": 2.3, "avg_execution_time_seconds": 0.05},
        "distilbert-base-cased-distilled-squad": {"task": "question_answering", "flavour": "fast",
                                                    "error_pct": 1.0, "avg_execution_time_seconds": 0.016},
    }
    result = build_task_flavours(stats)
    assert set(result.keys()) == {"text_generation", "question_answering"}

    tg = {f["name"]: f for f in result["text_generation"]}
    assert tg["Fast"]["error"] == 5.1
    assert tg["Fast"]["duration"] == 20  # 0.02s -> 20ms
    assert tg["Balanced"]["duration"] == 50

    assert len(result["question_answering"]) == 1


def test_duration_is_never_zero_for_very_fast_models():
    stats = {"m": {"task": "ner", "flavour": "fast", "error_pct": 0.0, "avg_execution_time_seconds": 0.0001}}
    result = build_task_flavours(stats)
    assert result["ner"][0]["duration"] == 1


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
    result = build_task_flavours(stats)
    assert len(result["question_answering"]) == 1
    assert result["question_answering"][0]["error"] == 13.5


def test_default_error_threshold_is_75_percent_between_min_and_max():
    flavours = [{"name": "Accurate", "error": 10.0}, {"name": "Fast", "error": 20.0}]
    assert default_error_threshold(flavours, 0.75) == 17.5


def test_default_error_threshold_position_zero_is_the_minimum():
    flavours = [{"name": "Accurate", "error": 10.0}, {"name": "Fast", "error": 20.0}]
    assert default_error_threshold(flavours, 0.0) == 10.0
