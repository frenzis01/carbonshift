#!/usr/bin/env python3
"""Builds and runs a multi-slot "fake time" emulation plan against a running
client server. Requires carbonshift (`MANUAL_CLOCK=1`) and the executor
(`EXECUTOR_MANUAL_CLOCK=1`) to also be running in manual-clock mode — see
README.md "Emulazione a tempo fittizio".

Usage:
    python scripts/run_emulation.py --slots 4 --per-slot 3 --slot-minutes 30
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import requests
import logging

# Use the root logger for simplicity
logger = logging.getLogger()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.carbonshift_client import CarbonshiftError, register_task  # noqa: E402
from app.plan_builder import build_requests  # noqa: E402
from scripts.push_flavours import DEFAULT_MODEL_STATS, build_task_flavours, default_error_threshold  # noqa: E402


def _ensure_task_registered(task: str, threshold_position: float) -> None:
    """Carbonshift's task registry (flavours + error threshold, see
    `push_flavours.py`) is in-memory only: a carbonshift restart between a
    `push_flavours.py` run and this emulation silently loses it, falling
    back to the built-in defaults (e.g. `max_error_threshold=4%`, unrelated
    to any calibrated flavour). Re-register here so every emulation run is
    self-contained regardless of restarts, instead of silently using stale
    settings."""
    if not DEFAULT_MODEL_STATS.exists():
        return
    flavours = build_task_flavours(json.loads(DEFAULT_MODEL_STATS.read_text())).get(task)
    if not flavours:
        return
    threshold = default_error_threshold(flavours, threshold_position)
    try:
        register_task(task, flavours, max_error_threshold=threshold)
        print(f"registered task={task}: {len(flavours)} flavours, max_error_threshold={threshold:.2f}%")
        logger.info(f"registered task={task}: {len(flavours)} flavours, max_error_threshold={threshold:.2f}%")
        
    except CarbonshiftError as exc:
        print(f"warning: could not register task={task} on carbonshift ({exc}) — using its defaults")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-url", default="http://localhost:8100")
    parser.add_argument("--executor-url", default="http://localhost:9000")
    parser.add_argument("--task", default="text_generation",
                         choices=["text_generation", "ner", "question_answering"])
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--per-slot", type=int, default=2)
    parser.add_argument("--slot-minutes", type=float, default=30.0)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--seed", type=int, default=None,
                         help="Fix for a reproducible run; omitted = a fresh random seed every run.")
    parser.add_argument("--threshold-position", type=float, default=0.75,
                         help="0-1 fraction between the task's min and max calibrated error (default: 0.75).")
    parser.add_argument("--no-register", action="store_true",
                         help="Skip (re-)registering calibrated flavours/threshold on carbonshift before sending.")
    args = parser.parse_args()

    if not args.no_register:
        _ensure_task_registered(args.task, args.threshold_position)

    seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)
    print(f"seed={seed}" + (" (random)" if args.seed is None else " (fixed)"))

    plan = build_requests(args.task, args.slots * args.per_slot, args.per_slot,
                           args.slot_minutes, args.source, seed)
    resp = requests.post(f"{args.client_url}/run/send-plan", json={
        "requests": plan, "slot_minutes": args.slot_minutes, "mode": "emulated",
        "executor_url": args.executor_url,
    })
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))
    print("\nWatch progress with URL:")
    print(f"  curl {args.client_url}/requests")
    print(f"  curl {args.client_url}/metrics/summary")
    print(f"  curl {args.client_url}/v1/stats")
    print(f"  curl {args.client_url}/v1/tasks/<task_id>")


if __name__ == "__main__":
    main()
