#!/usr/bin/env python3
"""Pushes the calibrated cold data (`model_stats.json`, produced by
`executor/scripts/calibrate_models.py`) to carbonshift as per-task flavour
registrations (`POST /v1/tasks`), so the DP solver schedules each task's
requests among its own real measured (error %, cost) flavours instead of
carbonshift's built-in default.

`duration` is sent in *milliseconds* (rounded, minimum 1): real model
execution times are almost always well under carbonshift's built-in
default flavours' 10-60s range, and `duration` must be a whole number.
This only affects the *absolute* gCO2 magnitude for calibrated tasks (not
directly comparable to the default task's) — `carbon_saving_pct` stays
correct because a task's own baseline and actual cost always use the same
flavour set (and therefore the same duration unit).

Also registers a per-task `max_error_threshold`: carbonshift's own global
default (4%) is tuned for a generic case and can be far stricter than what
any of a task's real calibrated flavours can achieve (e.g. text_generation's
cheapest flavour may sit at 20-40% error), which would make that task's
cheaper flavours permanently infeasible. Default: `--threshold-position`
(0-1, default 0.75) of the way between the task's min and max calibrated
error — e.g. accurate=10%, fast=20% -> threshold=17.5%. Pass
`--threshold-position` to move it, or edit the registered value directly
via another `POST /v1/tasks` call if you need something not on that line.

Usage:
    python scripts/push_flavours.py
    CARBONSHIFT_URL=http://localhost:8080 python scripts/push_flavours.py --model-stats model_stats.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.carbonshift_client import CarbonshiftError, register_task  # noqa: E402
from app.config import settings  # noqa: E402

DEFAULT_MODEL_STATS = Path(__file__).resolve().parent.parent / "model_stats.json"


def build_task_flavours(stats: dict) -> dict[str, list[dict]]:
    """Groups `model_stats.json` entries by task and converts each into a
    carbonshift `Flavour` dict (`name`, `error`, `duration`).

    If a task/flavour was calibrated for more than one model (e.g. the
    configured model was swapped after an earlier calibration run — old
    entries stay in `model_stats.json`, keyed by model id, by design), only
    the most recently measured entry for that (task, flavour) is used, so a
    stale model's numbers never get pushed alongside its replacement's."""
    by_task: dict[str, dict[str, dict]] = defaultdict(dict)
    for entry in stats.values():
        existing = by_task[entry["task"]].get(entry["flavour"])
        if existing is None or entry["measured_at"] > existing["measured_at"]:
            by_task[entry["task"]][entry["flavour"]] = entry

    return {
        task_id: [
            {
                "name": e["flavour"].capitalize(),
                "error": e["error_pct"],
                "duration": max(round(e["avg_execution_time_seconds"] * 1000), 1),
            }
            for e in entries_by_flavour.values()
        ]
        for task_id, entries_by_flavour in by_task.items()
    }


def default_error_threshold(flavours: list[dict], position: float) -> float:
    """`position` (0-1) of the way between a task's min and max calibrated
    flavour error — e.g. position=0.75, errors [10%, 20%] -> 17.5%."""
    errors = [f["error"] for f in flavours]
    return min(errors) + position * (max(errors) - min(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-stats", default=str(DEFAULT_MODEL_STATS))
    parser.add_argument("--threshold-position", type=float, default=0.75,
                         help="0-1 fraction between each task's min and max calibrated error (default: 0.75).")
    args = parser.parse_args()

    stats_path = Path(args.model_stats)
    if not stats_path.exists():
        raise SystemExit(f"{stats_path} not found — run executor/scripts/calibrate_models.py first")
    stats: dict = json.loads(stats_path.read_text())
    if not stats:
        raise SystemExit(f"{stats_path} is empty — run executor/scripts/calibrate_models.py first")

    for task_id, flavours in build_task_flavours(stats).items():
        threshold = default_error_threshold(flavours, args.threshold_position)
        try:
            register_task(task_id, flavours, max_error_threshold=threshold)
        except CarbonshiftError as exc:
            print(f"task={task_id}: FAILED ({exc})")
            continue
        print(f"task={task_id}: registered {len(flavours)} flavours (max_error_threshold={threshold:.2f}%) "
              f"on {settings.carbonshift_url}")
        for f in flavours:
            print(f"  {f['name']}: error={f['error']:.2f}% duration={f['duration']}ms")


if __name__ == "__main__":
    main()
