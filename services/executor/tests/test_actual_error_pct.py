"""`run_task`'s `actual_error_pct`/`quality_score` conventions (see its
docstring for why text_generation uses confidence-ratio instead of
word-overlap F1). Mocks `_run_single` so no real model is needed."""
from __future__ import annotations

import pytest

from app import inference
from app.config import Flavour, Task


def test_accurate_flavour_gets_zero_error_and_default_quality_one(monkeypatch):
    monkeypatch.setattr(inference.settings, "compute_quality_baseline", True)
    monkeypatch.setattr(inference, "_run_single", lambda task, flavour, task_input: {
        "output": {"generated_text": "hello"}, "model": "m", "confidence": 0.9, "execution_time_seconds": 0.1,
    })

    result = inference.run_task(Task.TEXT_GENERATION, Flavour.ACCURATE, {"prompt": "hi", "max_new_tokens": 5})

    assert result["actual_error_pct"] == 0.0
    assert result["quality_score"] == 1.0


def test_text_generation_uses_confidence_ratio_for_actual_error(monkeypatch):
    def fake_run_single(task, flavour, task_input):
        if flavour == Flavour.ACCURATE:
            return {"output": {"generated_text": "hi X"}, "model": "acc", "confidence": 0.8,
                    "execution_time_seconds": 0.2}
        return {"output": {"generated_text": "hi Y"}, "model": "primary", "confidence": 0.4,
                "execution_time_seconds": 0.1}

    monkeypatch.setattr(inference, "_run_single", fake_run_single)
    monkeypatch.setattr(inference.settings, "compute_quality_baseline", True)

    result = inference.run_task(Task.TEXT_GENERATION, Flavour.FAST, {"prompt": "hi", "max_new_tokens": 5})

    assert result["actual_error_pct"] == pytest.approx(50.0)  # (0.8 - 0.4) / 0.8 * 100


def test_qa_uses_quality_score_for_actual_error(monkeypatch):
    monkeypatch.setattr(inference, "_run_single", lambda task, flavour, task_input: {
        "output": {"answer": "Paris"}, "model": "m", "confidence": 0.9, "execution_time_seconds": 0.1,
    })
    monkeypatch.setattr(inference.settings, "compute_quality_baseline", True)

    result = inference.run_task(Task.QUESTION_ANSWERING, Flavour.FAST,
                                 {"question": "q", "context": "c", "reference_answer": "Paris"})

    assert result["quality_score"] == 1.0
    assert result["actual_error_pct"] == 0.0
