"""Model registry (task -> flavour -> HF model) and runtime settings.

Model choices are deliberately small/fast so they run comfortably on
commodity hardware (tested target: i7-7700, 16GB RAM, GTX 1060 3GB VRAM) —
see README.md for size/VRAM notes and download instructions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class Task:
    TEXT_GENERATION = "text_generation"
    NER = "ner"
    QUESTION_ANSWERING = "question_answering"


ALL_TASKS = [Task.TEXT_GENERATION, Task.NER, Task.QUESTION_ANSWERING]


class Flavour:
    ACCURATE = "accurate"
    BALANCED = "balanced"
    FAST = "fast"


ALL_FLAVOURS = [Flavour.ACCURATE, Flavour.BALANCED, Flavour.FAST]

# task -> flavour -> (huggingface pipeline task name, model id)
MODEL_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    Task.TEXT_GENERATION: {
        Flavour.FAST: ("text-generation", "distilgpt2"),
        Flavour.BALANCED: ("text-generation", "gpt2"),
        Flavour.ACCURATE: ("text-generation", "gpt2-medium"),
    },
    Task.NER: {
        Flavour.FAST: ("ner", "dslim/distilbert-NER"),
        Flavour.BALANCED: ("ner", "dslim/bert-base-NER"),
        Flavour.ACCURATE: ("ner", "dslim/bert-large-NER"),
    },
    Task.QUESTION_ANSWERING: {
        Flavour.FAST: ("question-answering", "distilbert-base-cased-distilled-squad"),
        # Was "deepset/bert-base-cased-squad2": empirically much worse than
        # even Fast on squad_v2 (~35% vs ~23% error over the same sample) —
        # the uncased checkpoint (matching Accurate's own uncased family)
        # measures ~13%, correctly between Accurate and Fast.
        # Flavour.BALANCED: ("question-answering", "deepset/bert-base-cased-squad2"),
        # Flavour.BALANCED: ("question-answering", "deepset/bert-base-uncased-squad2"),
        Flavour.BALANCED: ("question-answering", "deepset/roberta-base-squad2"),
        
        Flavour.ACCURATE: ("question-answering", "deepset/bert-large-uncased-whole-word-masking-squad2"),
    },
}


def resolve_model(task: str, flavour: str) -> tuple[str, str]:
    try:
        return MODEL_REGISTRY[task][flavour]
    except KeyError as exc:
        raise ValueError(f"unknown task/flavour combination: {task!r}/{flavour!r}") from exc


@dataclass
class Settings:
    host: str = os.environ.get("EXECUTOR_HOST", "0.0.0.0")
    port: int = int(os.environ.get("EXECUTOR_PORT", "9000"))
    # "auto" | "cpu" | "cuda"
    device: str = os.environ.get("EXECUTOR_DEVICE", "auto")
    # Bounds VRAM/RAM usage: only this many (task, flavour) pipelines are
    # kept loaded at once; least-recently-used is evicted beyond this.
    max_loaded_models: int = int(os.environ.get("EXECUTOR_MAX_LOADED_MODELS", "3"))
    poll_interval_seconds: float = float(os.environ.get("EXECUTOR_POLL_INTERVAL_SECONDS", "0.5"))
    metrics_path: str = os.environ.get("EXECUTOR_METRICS_PATH", "data/metrics.jsonl")
    callback_timeout_seconds: float = float(os.environ.get("EXECUTOR_CALLBACK_TIMEOUT_SECONDS", "10"))
    # Test/emulation mode: the queue's clock never advances with real wall
    # time, only via POST /admin/advance-slot (mirrors carbonshift's own
    # MANUAL_CLOCK) — never enable in production.
    manual_clock: bool = os.environ.get("EXECUTOR_MANUAL_CLOCK", "0") == "1"
    slot_minutes: float = float(os.environ.get("EXECUTOR_SLOT_MINUTES", "30"))
    # When true (default), any non-"accurate" flavour also runs a same-input
    # "shadow" Accurate-flavour pass to (a) get an empirical
    # baseline_execution_time_seconds/baseline_model and (b) derive a
    # quality_score by comparing outputs when no reference* was supplied.
    # Roughly doubles executor compute per request — disable for large-scale
    # runs that only care about actual scheduling/carbon behavior.
    compute_quality_baseline: bool = os.environ.get("EXECUTOR_COMPUTE_QUALITY_BASELINE", "1") == "1"


settings = Settings()
