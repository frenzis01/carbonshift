#!/usr/bin/env python3
"""Registers calibrated QoS profiles from `model_stats.json`.

Each task receives a stable, versioned profile ID (for example,
`question_answering-calibrated-v1`). Requests use that profile ID explicitly;
the executor task kind remains a separate field.

`duration` is sent in *milliseconds* (rounded, minimum 1): real model
execution times are almost always well under carbonshift's built-in
default flavours' 10-60s range, and `duration` must be a whole number.
This only affects the *absolute* gCO2 magnitude for calibrated tasks (not
directly comparable to the default task's) — `carbon_saving_pct` stays
correct because a task's own baseline and actual cost always use the same
flavour set (and therefore the same duration unit).

Each profile also receives a `max_error_threshold`: Carbonshift's built-in
default (4%) is tuned for a generic case and can be far stricter than what
any of a task's real calibrated flavours can achieve (e.g. text_generation's
cheapest flavour may sit at 20-40% error), which would make that task's
cheaper flavours permanently infeasible. Default: `--threshold-position`
(0-1, default 0.75) of the way between the task's min and max calibrated
error — e.g. accurate=10%, fast=20% -> threshold=17.5%. Pass
`--threshold-position` to move it. Profiles are immutable: use a new profile
version if you change their policy.

Usage:
    python scripts/push_flavours.py
    CARBONSHIFT_URL=http://localhost:8080 python scripts/push_flavours.py --model-stats model_stats.json
"""
from __future__ import annotations

import argparse
import json
import sys
import logging
from pathlib import Path

logger = logging.getLogger("client.push_flavours")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.carbonshift_client import CarbonshiftError, register_qos_profile  # noqa: E402
from app.config import settings  # noqa: E402
from app.qos_profiles import (  # noqa: E402
    DEFAULT_MODEL_STATS,
    build_task_flavours,
    build_task_profiles,
    default_error_threshold,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-stats", default=str(settings.qos_profile_stats_path))
    parser.add_argument("--threshold-position", type=float, default=settings.qos_profile_threshold_position,
                         help="0-1 fraction between each task's min and max calibrated error (default: 0.75).")
    parser.add_argument("--profile-version", default=settings.qos_profile_version,
                         help="Stable version suffix; bump it when calibration changes an immutable profile.")
    args = parser.parse_args()

    stats_path = Path(args.model_stats)
    if not stats_path.exists():
        raise SystemExit(f"{stats_path} not found — run executor/scripts/calibrate_models.py first")
    stats: dict = json.loads(stats_path.read_text())
    if not stats:
        raise SystemExit(f"{stats_path} is empty — run executor/scripts/calibrate_models.py first")

    failures: list[str] = []
    for task_id, profile in build_task_profiles(
        stats,
        args.threshold_position,
        args.profile_version,
    ).items():
        try:
            register_qos_profile(profile)
        except CarbonshiftError as exc:
            print(f"profile={profile['profile_id']}: FAILED ({exc})")
            failures.append(profile["profile_id"])
            continue
        logger.info(
            "registered QoS profile=%s task_kind=%s max_error_threshold=%.2f%%",
            profile["profile_id"],
            task_id,
            profile["max_error_threshold"],
        )
        print(
            f"profile={profile['profile_id']} task_kind={task_id} "
            f"max_error_threshold={profile['max_error_threshold']:.2f}%"
        )
        for f in profile["flavours"]:
            print(f"  {f['name']}: error={f['error']:.2f}% duration={f['duration']}ms")

    if failures:
        raise SystemExit(f"failed to register {len(failures)} QoS profile(s): {', '.join(failures)}")


if __name__ == "__main__":
    main()
