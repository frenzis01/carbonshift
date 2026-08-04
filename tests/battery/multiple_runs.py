#!/usr/bin/env python3
"""
Run `run_battery.py` across several PHASE-AWARE parameter grids.

Each phase of the Rust battery harness (see run_battery.py's module docstring)
is sensitive to a different subset of runtime knobs:

  - DP (min_error_greedy/carryover/forecast) and greedy_singleton (online,
    N=1 only): sensitive to `rollback_max_consecutive` and
    `max_batch_solver_parallelism`; NOT sensitive to `online_swarm_mode`.
  - bandit / ant_colony (online, swarm-based): sensitive to
    `online_swarm_mode` and `max_batch_solver_parallelism`; NOT sensitive to
    `rollback_max_consecutive` (the swarm commit path bypasses rollback).
  - offline strategies (greedy_cheapest, bandit, ant_colony offline variants):
    insensitive to all of the above — a single run per scenario suffices.

Sweeping every knob against every phase (the old flat PARAM_GRID behaviour)
therefore re-runs each phase many times for knobs that cannot change its
result. This script instead defines one `PhaseGrid` per phase, each pairing
the phase's *content* (which batch_sizes/strategies to exercise — the other
phases are left empty so run_battery.py skips them) with only the runtime
knobs that phase actually depends on.

For every grid point the script:
  1. loads `battery_config.json`
  2. overwrites it with the phase's content + that grid point's knob values
  3. saves it back to `battery_config.json`
  4. invokes `python tests/battery/run_battery.py`

Usage (from the repository root):
    python tests/battery/multiple_runs.py
    python tests/battery/multiple_runs.py --phase dp online offline   # subset
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# ─── paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
BATTERY_DIR = REPO_ROOT / "tests" / "battery"
CONFIG_PATH = BATTERY_DIR / "battery_config.json"
RUN_BATTERY_SCRIPT = BATTERY_DIR / "run_battery.py"


@dataclass
class PhaseGrid:
    """One independently-runnable phase of the battery.

    `content` fixes which batch_sizes/strategies are exercised (the phases
    NOT covered by this grid are left empty so run_battery.py's per-phase
    gates skip them entirely — see `run_dp`/`run_online`/`run_offline` in
    run_battery.py). `knob_grid` lists only the runtime-knob combinations
    this phase is actually sensitive to; every other knob stays at
    `battery_config.json`'s existing value.
    """

    name: str
    content: Dict[str, Any]
    knob_grid: List[Dict[str, Any]] = field(default_factory=lambda: [{}])


# ─── phase content (which strategies/batch sizes each phase exercises) ────────
DP_CONTENT: Dict[str, Any] = {
    # "batch_sizes": [1, 4, 6, 8, 10, 12, 16, 22],
    # "infeasibility_modes": ["min_error_greedy", "carryover", "forecast"],
    # "batch_sizes": [6, 8, 10, 12, 16, 22],
    # "batch_sizes": [8,10,22],
    "batch_sizes": [1,4,6,8,22],
    "infeasibility_modes": ["min_error_greedy","carryover"],
    "online_strategies": [],
    "additional_strategies": [],
}

SWARM_ONLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": ["bandit", "ant_colony"],
    "online_batch_sizes": [1,8,22],
    "additional_strategies": [],
}

GREEDY_SINGLETON_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": ["greedy_singleton"],
    # online_batch_sizes is irrelevant here: main.rs forces N=1 for
    # greedy_singleton regardless of what's requested.
    "additional_strategies": [],
}

OFFLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": [],
    "additional_strategies": ["greedy_cheapest", "bandit", "ant_colony"],
}

# ─── phase-specific knob grids ─────────────────────────────────────────────────
# DP + greedy_singleton: rollback x parallelism (swarm_mode is irrelevant to
# both, so it is left fixed at whatever battery_config.json already has).
_ROLLBACK_X_PARALLELISM_X_THRESHOLD = [
    {"rollback_max_consecutive": rollback, "max_batch_solver_parallelism": parallelism, "max_error_threshold": threshold}
    # for rollback in (0, 4)
    # for parallelism in (1, 8, 20)
    # for max_error_threshold in (3.0, 3.5, 4.0, 4.5)
    for rollback in (0,)
    for parallelism in (1,8,14,20)
    for threshold in (4.0,)
]

# bandit/ant_colony: parallelism x swarm_mode (rollback is irrelevant, left
# fixed).
_PARALLELISM_X_SWARM_MODE = [
    {"max_batch_solver_parallelism": parallelism, "online_swarm_mode": swarm_mode}
    # for parallelism in (1, 8, 20)
    for parallelism in (1, 8, 20)
    # for swarm_mode in ("serialized", "merge")
    for swarm_mode in ("merge",)
]

PHASES: Dict[str, PhaseGrid] = {
    "dp": PhaseGrid("dp", DP_CONTENT, _ROLLBACK_X_PARALLELISM_X_THRESHOLD),
    "online": PhaseGrid("online", SWARM_ONLINE_CONTENT, _PARALLELISM_X_SWARM_MODE),
    "greedy_singleton": PhaseGrid(
        "greedy_singleton", GREEDY_SINGLETON_CONTENT, _ROLLBACK_X_PARALLELISM_X_THRESHOLD
    ),
    # Offline strategies don't depend on any runtime knob: a single run
    # (empty knob override, i.e. keep battery_config.json's own defaults)
    # covers every scenario.
    "offline": PhaseGrid("offline", OFFLINE_CONTENT, [{}]),
}

# Which phases to run when `--phase` is not given.
DEFAULT_PHASES = ["dp", "online", "greedy_singleton", "offline"]


def load_config(path: Path) -> Dict[str, Any]:
    """Load the battery configuration JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path: Path, cfg: Dict[str, Any]) -> None:
    """Save the battery configuration JSON preserving a readable format."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def run_battery() -> int:
    """Invoke run_battery.py from the repository root."""
    cmd = [sys.executable, str(RUN_BATTERY_SCRIPT.relative_to(REPO_ROOT))]
    print(f"Running: {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=REPO_ROOT)


def run_phase(base_cfg: Dict[str, Any], phase: PhaseGrid, battery_id_prefix: str) -> int:
    """Run every grid point of one phase, returning the last non-zero exit
    status seen (0 if all grid points succeeded)."""
    overall_status = 0
    total = len(phase.knob_grid)

    for idx, knobs in enumerate(phase.knob_grid, start=1):
        cfg = dict(base_cfg)
        cfg.update(phase.content)

        # `max_error_threshold` is a per-scenario override in battery_config.json
        # (nested under "scenarios"[i]), not a top-level runtime knob — apply it
        # to every scenario entry instead of setting a (meaningless) root key.
        knobs = dict(knobs)
        max_error_threshold = knobs.pop("max_error_threshold", None)
        cfg.update(knobs)
        if max_error_threshold is not None:
            cfg["scenarios"] = [
                {**scenario, "max_error_threshold": max_error_threshold}
                for scenario in cfg.get("scenarios", [])
            ]

        cfg["battery_id"] = f"{battery_id_prefix}_{phase.name}_{idx - 1}"

        print("\n" + "=" * 70)
        print(f"Phase '{phase.name}' — run {idx}/{total}: knobs={knobs}")
        print("=" * 70)

        save_config(CONFIG_PATH, cfg)
        status = run_battery()
        if status != 0:
            print(
                f"WARNING: run_battery.py exited with status {status} "
                f"for phase={phase.name} run {idx}",
                file=sys.stderr,
            )
            overall_status = status

    return overall_status


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        nargs="+",
        choices=list(PHASES.keys()),
        default=DEFAULT_PHASES,
        help="Which phase grid(s) to run (default: all).",
    )
    parser.add_argument(
        "--battery-id-prefix",
        default="cfg_test0",
        help="Prefix used to build each run's battery_id "
        "(final id is '<prefix>_<phase>_<grid-index>').",
    )
    args = parser.parse_args(argv)

    if not CONFIG_PATH.exists():
        print(f"Configuration file not found: {CONFIG_PATH}", file=sys.stderr)
        return 1

    base_cfg = load_config(CONFIG_PATH)
    overall_status = 0
    try:
        for phase_name in args.phase:
            status = run_phase(base_cfg, PHASES[phase_name], args.battery_id_prefix)
            if status != 0:
                overall_status = status
    finally:
        # Always restore the on-disk config to its pre-run state so repeated
        # invocations (or manual edits) aren't clobbered by the last grid
        # point's overrides.
        save_config(CONFIG_PATH, base_cfg)

    print("\n" + "=" * 70)
    print(f"All requested phases ({', '.join(args.phase)}) completed.")
    print("=" * 70)
    return overall_status


if __name__ == "__main__":
    sys.exit(main())
