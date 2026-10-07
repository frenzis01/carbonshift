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

from app.carbonshift_client import (  # noqa: E402
    CarbonshiftError,
    force_set_global_capacity_tiers,
    register_qos_profile,
)
from app.config import settings  # noqa: E402
from app.plan_builder import build_requests  # noqa: E402
from app.qos_profiles import build_task_profiles, load_configured_profiles  # noqa: E402


def _ensure_profile_registered(
    task: str,
    profile_id: str,
    threshold_position: float,
    profile_version: str,
) -> None:
    """Re-register the same stable profile after Carbonshift's in-memory reset."""
    stats_path = Path(settings.qos_profile_stats_path)
    generated_id = _default_profile_id(task, profile_version)
    if profile_id == generated_id and not stats_path.exists():
        raise SystemExit(
            f"{stats_path} not found; cannot register {profile_id!r}. "
            "Use --no-register with a profile registered by another client, or omit the profile ID."
        )
    if profile_id == generated_id:
        profiles = build_task_profiles(
            json.loads(stats_path.read_text(encoding="utf-8")),
            threshold_position,
            profile_version,
        )
        profile = profiles.get(task)
        if profile is None:
            raise SystemExit(f"no calibrated flavours for task {task!r}; cannot register {profile_id!r}")
    else:
        # A custom ID must have a complete matching definition in the
        # client's local catalog, so this client can restore it later too.
        profile = next(
            (
                candidate
                for candidate in load_configured_profiles()
                if candidate["profile_id"] == profile_id
            ),
            None,
        )
        if profile is None:
            raise SystemExit(
                f"custom profile {profile_id!r} has no local definition; "
                "add it to CLIENT_QOS_PROFILE_DEFINITIONS_PATH or use --no-register"
            )
        if profile["task_kind"] != task:
            raise SystemExit(
                f"profile {profile_id!r} is for task kind {profile['task_kind']!r}, not {task!r}"
            )
    try:
        register_qos_profile(profile)
    except CarbonshiftError as exc:
        raise SystemExit(f"could not register QoS profile {profile_id!r}: {exc}") from exc

    print(
        f"registered qos_profile_id={profile_id} task_kind={task}: "
        f"{len(profile['flavours'])} flavours, "
        f"max_error_threshold={profile['max_error_threshold']:.2f}%"
    )
    logger.info(
        "registered qos_profile_id=%s task_kind=%s with %d flavours",
        profile_id,
        task,
        len(profile["flavours"]),
    )


def _default_profile_id(task: str, profile_version: str) -> str:
    """Use the same stable versioned ID as `push_flavours.py`."""
    return f"{task}-calibrated-{profile_version}"


def build_capacity_tiers(requests_per_slot: int) -> list[dict[str, int | float | None]]:
    """Build the old emulation pricing ladder from its requests-per-slot setting.

    The ladder is global and affects every client/profile using Carbonshift.
    The second finite bound is kept strictly above the first even for tiny
    workloads (for example, `--per-slot 1`).
    """
    base = max(1, requests_per_slot)
    middle = max(base + 1, int(base * 1.5))
    return [
        {"max_requests": base, "multiplier": 1.0},
        {"max_requests": middle, "multiplier": 1.5},
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
    parser.add_argument("--pattern", default="sinusoidal", choices=["flat", "sinusoidal", "random"],
                         help="Pattern for request generation per slot (default: flat).")
    parser.add_argument("--slot-minutes", type=float, default=30.0)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--seed", type=int, default=None,
                         help="Fix for a reproducible run; omitted = a fresh random seed every run.")
    parser.add_argument("--qos-profile-id", default=None,
                         help="Stable profile ID to register and send; omit to use the task-kind default.")
    parser.add_argument("--profile-version", default=settings.qos_profile_version,
                         help="Version suffix for the auto-generated calibrated profile ID.")
    parser.add_argument("--threshold-position", type=float, default=settings.qos_profile_threshold_position,
                         help="0-1 fraction between the task's min and max calibrated error (default: 0.75).")
    parser.add_argument("--no-register", action="store_true",
                         help="Do not register a profile; omit --qos-profile-id to use the task-kind default.")
    parser.add_argument(
        "--no-global-capacity-tiers",
        action="store_true",
        help=(
            "AVOID setting Carbonshift's global capacity ladder from --per-slot before submitting. "
            "Setting them instead affects all profiles."
        ),
    )
    parser.add_argument("--poll-interval", type=float, default=1.0,
                         help="Seconds between progress checks while advancing the clock.")
    parser.add_argument("--timeout", type=float, default=600.0,
                         help="Total timeout in seconds for the emulation to finish.")
    args = parser.parse_args()

    if not args.no_global_capacity_tiers:
        tiers = build_capacity_tiers(args.per_slot)
        try:
            force_set_global_capacity_tiers(tiers)
        except CarbonshiftError as exc:
            raise SystemExit(
                "could not set global capacity tiers; the Carbonshift endpoint may still be "
                f"the documented Rust scaffold: {exc}"
            ) from exc
        print(f"set global capacity tiers from --per-slot={args.per_slot}: {tiers}")

    qos_profile_id = args.qos_profile_id
    if not args.no_register:
        stats_path = Path(settings.qos_profile_stats_path)
        if not stats_path.exists() and qos_profile_id is None:
            logger.warning(
                "%s is missing; sending without qos_profile_id so Carbonshift uses its task-kind default",
                stats_path,
            )
        else:
            if qos_profile_id is None:
                if args.profile_version != settings.qos_profile_version:
                    raise SystemExit(
                        "--profile-version must match CLIENT_QOS_PROFILE_VERSION on the client service "
                        "so the same stable profile can be restored after a scheduler restart"
                    )
                if args.threshold_position != settings.qos_profile_threshold_position:
                    raise SystemExit(
                        "--threshold-position must match CLIENT_QOS_PROFILE_THRESHOLD_POSITION "
                        "unless you also configure a new profile version on the client service"
                    )
            qos_profile_id = qos_profile_id or _default_profile_id(args.task, args.profile_version)
            _ensure_profile_registered(
                args.task,
                qos_profile_id,
                args.threshold_position,
                args.profile_version,
            )

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

    plan = build_requests(
        args.task,
        args.slots,
        args.per_slot,
        args.slot_minutes,
        args.source,
        seed,
        pattern=args.pattern,
        reference=reference,
        qos_profile_id=qos_profile_id,
    )
    resp = requests.post(f"{args.client_url}/run/send-plan", json={
        "requests": plan, "slot_minutes": args.slot_minutes, "mode": "emulated",
        "executor_url": args.executor_url,
    })
    resp.raise_for_status()
    print("Plan registered:", json.dumps(resp.json(), indent=2))

    total_expected = len(plan)
    print(f"\nDriving emulation: {args.slots} slots, {total_expected} requests total...")

    # time counter for measuring how long the emulation takes
    start_emulation = time.time()

    # First advance on provider triggers announce(slot 0) + rollover(slot 1).
    # Subsequent advances trigger rollover(slot 2, 3, ...).
    # max(1, args.slots - 1) advances cover all plan slots [0 .. args.slots - 1].
    advances_for_plan = max(1, args.slots - 1)
    advance_timeout = max(args.timeout, 180.0)
    for step in range(advances_for_plan):
        adv_resp = requests.post(f"{args.provider_url}/v1/advance-slot", json={}, timeout=advance_timeout)
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
                    requests.post(f"{args.provider_url}/v1/advance-slot", json={}, timeout=advance_timeout)
                except Exception as adv_err:
                    logger.debug("extra advance tick error: %s", adv_err)

        except Exception as e:
            logger.warning("progress check error: %s", e)

        time.sleep(args.poll_interval)
    else:
        print(f"\nWarning: timed out after {args.timeout}s waiting for emulation to finish.")
    
    end_emulation = time.time()
    print(f"Total emulation time: {end_emulation - start_emulation:.2f}s")

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
