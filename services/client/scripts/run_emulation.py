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
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Use the root logger for simplicity
logger = logging.getLogger()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.carbonshift_client import CarbonshiftError, register_task  # noqa: E402
from app.plan_builder import build_requests  # noqa: E402
from scripts.push_flavours import DEFAULT_MODEL_STATS, build_task_flavours, default_error_threshold  # noqa: E402


def _ensure_task_registered(task: str, threshold_position: float, requests_per_slot: int) -> None:
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
    capacity_tiers = build_capacity_tiers(requests_per_slot)  # Example value; adjust as needed
    try:
        register_task(task, flavours, max_error_threshold=threshold, capacity_tiers=capacity_tiers)
        print(f"registered task={task}: {len(flavours)} flavours, max_error_threshold={threshold:.2f}%")
        logger.info(f"registered task={task}: {len(flavours)} flavours, max_error_threshold={threshold:.2f}%")
        
    except CarbonshiftError as exc:
        print(f"warning: could not register task={task} on carbonshift ({exc}) — using its defaults")

def build_capacity_tiers(requests_per_slot: int) -> list[dict]:
    base = max(1, requests_per_slot)
    return [
        {"max_requests": base, "multiplier": 1.0},
        {"max_requests": base * 2, "multiplier": 1.5},
        {"max_requests": None, "multiplier": 5.0},
    ]

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-url", default="http://localhost:8100")
    parser.add_argument("--executor-url", default="http://localhost:9000")
    parser.add_argument("--provider-url", default=os.environ.get("PROVIDER_URL", "http://localhost:9100"))
    parser.add_argument("--task", default="text_generation",
                         choices=["text_generation", "ner", "question_answering"])
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--per-slot", type=int, default=20)
    parser.add_argument("--slot-minutes", type=float, default=30.0)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--seed", type=int, default=None,
                         help="Fix for a reproducible run; omitted = a fresh random seed every run.")
    parser.add_argument("--threshold-position", type=float, default=0.75,
                         help="0-1 fraction between the task's min and max calibrated error (default: 0.75).")
    parser.add_argument("--no-register", action="store_true",
                         help="Skip (re-)registering calibrated flavours/threshold on carbonshift before sending.")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                         help="Seconds between progress checks while advancing the clock.")
    parser.add_argument("--timeout", type=float, default=600.0,
                         help="Total timeout in seconds for the emulation to finish.")
    args = parser.parse_args()

    if not args.no_register:
        _ensure_task_registered(args.task, args.threshold_position, args.per_slot)

    seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)
    print(f"seed={seed}" + (" (random)" if args.seed is None else " (fixed)"))

    reference = None
    try:
        slot_resp = requests.get(f"{args.provider_url}/v1/slot", timeout=5)
        if slot_resp.status_code == 200:
            slot_info = slot_resp.json()
            reference = datetime.fromisoformat(slot_info["slot_start_utc"])
            print(f"Aligned plan with provider current_slot={slot_info['current_slot']} start={slot_info['slot_start_utc']}")
    except Exception as e:
        print(f"Notice: could not query provider at {args.provider_url}/v1/slot ({e}); using wall clock")

    plan = build_requests(args.task, args.slots * args.per_slot, args.per_slot,
                           args.slot_minutes, args.source, seed, reference=reference)
    resp = requests.post(f"{args.client_url}/run/send-plan", json={
        "requests": plan, "slot_minutes": args.slot_minutes, "mode": "emulated",
        "executor_url": args.executor_url,
    })
    resp.raise_for_status()
    print("Plan registered:", json.dumps(resp.json(), indent=2))

    total_expected = args.slots * args.per_slot
    print(f"\nDriving emulation: {args.slots} slots, {total_expected} requests total...")

    # First advance on provider triggers announce(slot 0) + rollover(slot 1).
    # Subsequent advances trigger rollover(slot 2, 3, ...).
    # max(1, args.slots - 1) advances cover all plan slots [0 .. args.slots - 1].
    advances_for_plan = max(1, args.slots - 1)
    for step in range(advances_for_plan):
        adv_resp = requests.post(f"{args.provider_url}/v1/advance-slot", json={}, timeout=60)
        adv_resp.raise_for_status()
        adv_data = adv_resp.json()
        print(f"  [Step {step + 1}/{advances_for_plan}] Advanced provider to slot {adv_data.get('current_slot')}")
        time.sleep(0.1)

    print("\nWaiting for all requests to complete...")
    start_wait = time.time()
    last_print = 0

    while time.time() - start_wait < args.timeout:
        try:
            prog = requests.get(f"{args.client_url}/metrics/progress", timeout=5).json()
            completed = prog.get("requests_completed", 0)
            failed = prog.get("requests_failed", 0)
            timed_out = prog.get("requests_timed_out", 0)
            resolved = completed + failed + timed_out
            sent = prog.get("requests_sent", 0)

            if time.time() - last_print >= 2.0:
                print(f"  Progress: {resolved}/{total_expected} resolved ({completed} completed, {failed} failed, {timed_out} timed out, {sent} sent)")
                last_print = time.time()

            if resolved >= total_expected and sent >= total_expected:
                print(f"\nAll {total_expected} requests resolved!")
                break

            # If requests have been submitted, but some are still pending or scheduled
            # in later slots, issue an extra advance to move the simulation forward!
            if sent > resolved:
                try:
                    requests.post(f"{args.provider_url}/v1/advance-slot", json={}, timeout=60)
                except Exception as adv_err:
                    logger.debug("extra advance tick error: %s", adv_err)

        except Exception as e:
            logger.warning("progress check error: %s", e)

        time.sleep(args.poll_interval)
    else:
        print(f"\nWarning: timed out after {args.timeout}s waiting for emulation to finish.")

    # Fetch and display final summary
    print("\n=== Final Metrics Summary ===")
    try:
        summary_resp = requests.get(f"{args.client_url}/metrics/summary", timeout=10)
        if summary_resp.status_code == 200:
            print(json.dumps(summary_resp.json(), indent=2))
        else:
            print(f"Summary returned HTTP {summary_resp.status_code}")
    except Exception as e:
        print(f"Could not fetch metrics summary: {e}")


if __name__ == "__main__":
    main()
