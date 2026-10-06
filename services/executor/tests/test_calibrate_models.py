"""Calibration rows must carry the metric identity used by their percentages."""
from __future__ import annotations

from scripts import calibrate_models


def test_calibration_persists_task_error_semantics(monkeypatch):
    monkeypatch.setattr(
        calibrate_models,
        "_load_examples",
        lambda task, count, seed, source: [{"input": {"question": "q"}}],
    )
    monkeypatch.setattr(
        calibrate_models,
        "run_task",
        lambda task, flavour, inputs: {
            "execution_time_seconds": 0.2,
            "quality_score": 0.75,
        },
    )
    monkeypatch.setattr(
        calibrate_models,
        "resolve_model",
        lambda task, flavour: ("question-answering", "qa-model"),
    )

    result = calibrate_models.calibrate_one(
        "question_answering",
        "fast",
        count=1,
        seed=1,
        source="synthetic",
    )

    assert result["error_semantics"] == "word-overlap-f1-v1"
    assert result["task"] == "question_answering"
    assert result["error_pct"] == 25.0
