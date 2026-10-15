#!/usr/bin/env python3
"""Run a multi-slot fake-time emulation against running client services.

By default, NER and question-answering plans are sent to clients on ports 8100
and 8101. Additional clients can be configured with repeated --client-url and
--task options. Requires the provider, CarbonShift, and executor in
manual-clock mode (see README.md "Emulazione a tempo fittizio").

Usage:
    python scripts/run_emulation_multi.py --slots 4 --per-slot 3
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# Use the root logger for simplicity
logger = logging.getLogger()

service_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(service_root))
shared_root = service_root.parent
if (shared_root / "common").is_dir():
    sys.path.insert(0, str(shared_root))

from common.snapshots import (  # noqa: E402
    SnapshotError,
    default_snapshot_directory,
    export_emulation_snapshot,
)
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


DEFAULT_CLIENT_URLS = ("http://localhost:8100", "http://localhost:8101")
DEFAULT_TASKS = ("ner", "question_answering")


@dataclass(frozen=True)
class EmulationTarget:
    client_url: str
    task: str
    qos_profile_id: str | None


def _resolve_targets(
    client_urls: list[str] | None,
    tasks: list[str] | None,
    qos_profile_ids: list[str] | None,
) -> list[EmulationTarget]:
    """Pair each configured client with its task and optional profile."""
    if client_urls is None and tasks is None:
        client_urls = list(DEFAULT_CLIENT_URLS)
        tasks = list(DEFAULT_TASKS)
    elif client_urls is None:
        client_urls = list(DEFAULT_CLIENT_URLS[:len(tasks or [])])
    elif tasks is None:
        tasks = list(DEFAULT_TASKS[:len(client_urls)])

    if len(client_urls) != len(tasks):
        raise ValueError("provide one --task for each --client-url")
    if not client_urls:
        raise ValueError("at least one --client-url and --task pair is required")
    if qos_profile_ids is not None and len(qos_profile_ids) != len(client_urls):
        raise ValueError("provide one --qos-profile-id for each client/task pair")

    normalized_urls = [url.strip().rstrip("/") for url in client_urls]
    if any(not url for url in normalized_urls):
        raise ValueError("--client-url cannot be empty")
    if len({url.rstrip("/") for url in normalized_urls}) != len(normalized_urls):
        raise ValueError("each client must have a unique --client-url")

    profile_ids = qos_profile_ids or [None] * len(normalized_urls)
    return [
        EmulationTarget(client_url, task, profile_id)
        for client_url, task, profile_id in zip(normalized_urls, tasks, profile_ids)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-url",
        dest="client_urls",
        action="append",
        default=None,
        help="Client service URL; repeat once per client (default: ports 8100 and 8101).",
    )
    parser.add_argument("--executor-url", default="http://localhost:9000")
    parser.add_argument("--provider-url", default=os.environ.get("PROVIDER_URL", "http://localhost:9100"))
    parser.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=None,
        choices=["text_generation", "ner", "question_answering"],
        help="Task kind paired with each --client-url; repeat once per client.",
    )
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--per-slot", type=int, default=20)
    parser.add_argument("--pattern", default="sinusoidal", choices=["flat", "sinusoidal", "random"],
                         help="Pattern for request generation per slot (default: flat).")
    parser.add_argument("--slot-minutes", type=float, default=settings.provider_slot_minutes)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--seed", type=int, default=None,
                         help="Fix for a reproducible run; omitted = a fresh random seed every run.")
    parser.add_argument(
        "--qos-profile-id",
        dest="qos_profile_ids",
        action="append",
        default=None,
        help="Stable profile ID paired with each client/task; repeat once per explicit profile.",
    )
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
    parser.add_argument(
        "--visualizer-url",
        default=os.environ.get("VISUALIZER_URL", "http://localhost:8500"),
        help="Visualizer URL used to capture chart-ready snapshot data.",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=default_snapshot_directory(),
        help="Persistent snapshot directory (default: SNAPSHOTS_DIR or ./snapshots).",
    )
    parser.add_argument("--snapshot-label", default=None, help="Optional human-readable snapshot name.")
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Do not export dashboard and summary snapshots when the run ends.",
    )
    args = parser.parse_args()

    try:
        targets = _resolve_targets(args.client_urls, args.tasks, args.qos_profile_ids)
    except ValueError as exc:
        parser.error(str(exc))

    if not args.no_global_capacity_tiers:
        total_per_slot = args.per_slot * len(targets)
        tiers = build_capacity_tiers(total_per_slot)
        try:
            force_set_global_capacity_tiers(tiers)
        except CarbonshiftError as exc:
            raise SystemExit(
                f"could not set global capacity tiers on Carbonshift: {exc}"
            ) from exc
        print(
            f"set global capacity tiers for {len(targets)} clients "
            f"({total_per_slot} requests/slot): {tiers}"
        )

    profile_ids: list[str | None] = []
    stats_path = Path(settings.qos_profile_stats_path)
    for target in targets:
        qos_profile_id = target.qos_profile_id
        if not args.no_register:
            if not stats_path.exists() and qos_profile_id is None:
                logger.warning(
                    "%s is missing; sending task=%s without qos_profile_id so Carbonshift "
                    "uses its task-kind default",
                    stats_path,
                    target.task,
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
                    qos_profile_id = _default_profile_id(target.task, args.profile_version)
                _ensure_profile_registered(
                    target.task,
                    qos_profile_id,
                    args.threshold_position,
                    args.profile_version,
                )
        profile_ids.append(qos_profile_id)

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

    total_expected = 0
    for index, (target, qos_profile_id) in enumerate(zip(targets, profile_ids)):
        client_seed = seed + index
        plan = build_requests(
            target.task,
            args.slots,
            args.per_slot,
            args.slot_minutes,
            args.source,
            client_seed,
            pattern=args.pattern,
            reference=reference,
            qos_profile_id=qos_profile_id,
        )
        resp = requests.post(
            f"{target.client_url}/run/send-plan",
            json={
                "requests": plan,
                "slot_minutes": args.slot_minutes,
                "mode": "emulated",
                "executor_url": args.executor_url,
            },
        )
        resp.raise_for_status()
        print(
            f"Plan registered for task={target.task} client={target.client_url} "
            f"(seed={client_seed}): {json.dumps(resp.json(), indent=2)}"
        )
        total_expected += len(plan)

    print(
        f"\nDriving emulation: {len(targets)} clients, {args.slots} slots each, "
        f"{total_expected} requests total..."
    )

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
        progress_by_client = []
        for target in targets:
            try:
                progress = requests.get(
                    f"{target.client_url}/metrics/progress",
                    timeout=5,
                ).json()
                progress_by_client.append((target, progress))
            except Exception as exc:
                logger.warning(
                    "progress check error for task=%s client=%s: %s",
                    target.task,
                    target.client_url,
                    exc,
                )

        completed = sum(progress.get("requests_completed", 0) for _, progress in progress_by_client)
        failed = sum(progress.get("requests_failed", 0) for _, progress in progress_by_client)
        timed_out = sum(progress.get("requests_timed_out", 0) for _, progress in progress_by_client)
        sent = sum(progress.get("requests_sent", 0) for _, progress in progress_by_client)
        resolved = completed + failed + timed_out

        if time.time() - last_print >= 2.0:
            client_details = "; ".join(
                f"{target.task}: {progress.get('requests_completed', 0) + progress.get('requests_failed', 0) + progress.get('requests_timed_out', 0)}"
                f"/{progress.get('requests_sent', 0)} resolved"
                for target, progress in progress_by_client
            )
            print(
                f"  Progress: {resolved}/{total_expected} resolved "
                f"({completed} completed, {failed} failed, {timed_out} timed out, {sent} sent)"
                + (f" [{client_details}]" if client_details else "")
            )
            last_print = time.time()

        if resolved >= total_expected and sent >= total_expected:
            print(f"\nAll {total_expected} requests resolved!")
            break

        # If any client has submitted requests that remain pending or scheduled
        # in later slots, issue an extra advance to move the simulation forward.
        if any(
            progress.get("requests_sent", 0)
            > progress.get("requests_completed", 0)
            + progress.get("requests_failed", 0)
            + progress.get("requests_timed_out", 0)
            for _, progress in progress_by_client
        ):
            try:
                requests.post(f"{args.provider_url}/v1/advance-slot", json={}, timeout=advance_timeout)
            except Exception as adv_err:
                logger.debug("extra advance tick error: %s", adv_err)

        time.sleep(args.poll_interval)
    else:
        print(f"\nWarning: timed out after {args.timeout}s waiting for emulation to finish.")
    
    end_emulation = time.time()
    print(f"Total emulation time: {end_emulation - start_emulation:.2f}s")

    # Fetch and display final summary
    print("\n=== Final Metrics Summary ===")
    summary_records: list[dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        print(f"\n--- task={target.task} client={target.client_url} ---")
        summary_record: dict[str, Any] = {
            "label": f"_{index}",
            "task": target.task,
            "client_url": target.client_url,
            "summary": None,
        }
        try:
            summary_resp = requests.get(f"{target.client_url}/metrics/summary", timeout=10)
            if summary_resp.status_code == 200:
                summary_record["summary"] = summary_resp.json()
                print(json.dumps(summary_record["summary"], indent=2))
            else:
                summary_record["error"] = f"summary returned HTTP {summary_resp.status_code}"
                print(f"Summary returned HTTP {summary_resp.status_code}")
        except (requests.RequestException, ValueError) as exc:
            summary_record["error"] = f"could not fetch metrics summary: {exc}"
            print(f"Could not fetch metrics summary: {exc}")
        summary_records.append(summary_record)

    if not args.no_snapshot:
        try:
            snapshot = export_emulation_snapshot(
                snapshots_dir=args.snapshot_dir,
                visualizer_url=args.visualizer_url,
                clients=summary_records,
                label=args.snapshot_label,
            )
        except (SnapshotError, OSError, TypeError, ValueError) as exc:
            raise SystemExit(f"emulation finished, but snapshot export failed: {exc}") from exc
        print(f"Snapshot saved: {snapshot['snapshot_filename']}")


if __name__ == "__main__":
    main()
