#!/usr/bin/env python3
"""One-off calibration: runs each configured (task, flavour) HuggingFace
model against a handful of examples to measure its *real* error % and
average execution time, then merges the results into the client's
cold-data JSON (keyed by `model_id`, so switching/adding models later never
loses older measurements for models no longer configured).

For question_answering/ner, error % comes from `quality_score` (word-overlap
F1 against ground truth). For text_generation there is no single "correct"
continuation, so word-overlap F1 (whether against literal source text or a
same-input shadow run of Accurate — both were tried) mostly measures "this
phrasing differs from that one", not quality; error % there instead comes
from `confidence` (self-supervised, i.e. how "unsurprised" the model is by
its own generated text) *relative to Accurate's own confidence* on the same
prompt — the standard way autoregressive LMs are compared (a distilled/
smaller model is measurably less confident on its own generation than its
teacher, without needing any ground truth).

Must run inside the executor's environment (needs torch/transformers).

`--source dataset` (recommended for meaningful numbers, needs `pip install
datasets` — see requirements-dev.txt) draws from hundreds/thousands of real
examples with ground truth, instead of the default `synthetic`'s ~5
hand-written examples per task repeated to fill `--samples` (still with
equal coverage — see `datasets.py::_cycled_sample` — but a small fixed pool
means a handful of per-example quirks can swing the whole average, e.g. one
flavour looking worse than a supposedly-lower one just because of which of
the 5 examples it stumbled on).

Usage:
    python scripts/calibrate_models.py --source dataset --samples 30
    python scripts/calibrate_models.py --samples 20 --output ../client/model_stats.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
CLIENT_DIR = Path(__file__).resolve().parent.parent.parent / "client"
DEFAULT_OUTPUT = CLIENT_DIR / "model_stats.json"

from app.config import ALL_FLAVOURS, ALL_TASKS, Flavour, Task, resolve_model  # noqa: E402
from app.inference import run_task  # noqa: E402


def _load_examples(task: str, count: int, seed: int, source: str) -> list[dict]:
    # Loaded by file path (not `sys.path` + `import app.datasets`) because
    # the client's package is also named `app`, same as the executor's own
    # — inserting both on `sys.path` would silently resolve to whichever
    # `app` package Python imported first instead of raising an ImportError.
    spec = importlib.util.spec_from_file_location("_client_datasets", CLIENT_DIR / "app" / "datasets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_examples(task, count, seed=seed, source=source)


def calibrate_one(task: str, flavour: str, count: int, seed: int, source: str) -> dict | None:
    examples = _load_examples(task, count, seed, source)
    errors: list[float] = []
    durations: list[float] = []
    for ex in examples:
        result = run_task(task, flavour, ex["input"])
        durations.append(result["execution_time_seconds"])

        if task == Task.TEXT_GENERATION:
            if flavour == Flavour.ACCURATE:
                continue  # it's the reference itself; error stays 0 (see below)
            accurate_confidence = run_task(task, Flavour.ACCURATE, ex["input"])["confidence"]
            confidence = result["confidence"]
            if accurate_confidence and confidence is not None:
                errors.append(max(0.0, (accurate_confidence - confidence) / accurate_confidence) * 100.0)
        elif result.get("quality_score") is not None:
            errors.append((1.0 - result["quality_score"]) * 100.0)

    if not durations:
        return None
    _, model_id = resolve_model(task, flavour)
    return {
        "model": model_id,
        "task": task,
        "flavour": flavour,
        # No ground truth/shadow comparison ever applies to "accurate" itself
        # (it *is* the reference) -> `errors` stays empty -> 0.0, matching
        # the existing "accurate = 0% error" convention.
        "error_pct": sum(errors) / len(errors) if errors else 0.0,
        "avg_execution_time_seconds": sum(durations) / len(durations),
        "sample_count": len(durations),
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--samples", type=int, default=30, help="Examples per (task, flavour) combination.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--tasks", nargs="+", default=ALL_TASKS, choices=ALL_TASKS)
    parser.add_argument("--flavours", nargs="+", default=ALL_FLAVOURS, choices=ALL_FLAVOURS)
    args = parser.parse_args()

    output_path = Path(args.output)
    stats: dict = json.loads(output_path.read_text()) if output_path.exists() else {}

    for task in args.tasks:
        for flavour in args.flavours:
            print(f"Calibrating task={task} flavour={flavour}...")
            entry = calibrate_one(task, flavour, args.samples, args.seed, args.source)
            if entry is None:
                print("  skipped: no samples produced")
                continue
            stats[entry["model"]] = entry
            print(f"  error_pct={entry['error_pct']:.2f} avg_execution_time_seconds={entry['avg_execution_time_seconds']:.4f}")

    output_path.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    print(f"\nWrote {len(stats)} model entries to {output_path}")


if __name__ == "__main__":
    main()
