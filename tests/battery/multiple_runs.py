#!/usr/bin/env python3
"""
Run `run_battery.py` across several PHASE-AWARE parameter grids.

Each phase of the Rust battery harness (see run_battery.py's module docstring)
is sensitive to a different subset of runtime knobs:

  - DP (min_error_greedy/carryover/forecast), singleton_best, and random
    selection modes (online, N=1 only): sensitive to `rollback_max_consecutive` and
    `max_batch_solver_parallelism`; NOT sensitive to `online_swarm_mode`.
  - bandit (online, N=1) and ant_colony (online, batch-size sweep): sensitive to
    `online_swarm_mode` and `max_batch_solver_parallelism`; NOT sensitive to
    `rollback_max_consecutive` (the swarm commit path bypasses rollback).
  - offline strategies (offline_singleton_best, offline_singleton_best_accurate, random,
    bandit, ant_colony):
    insensitive to all of the above — a single run per scenario suffices.

Sweeping every knob against every phase (the old flat PARAM_GRID behaviour)
therefore re-runs each phase many times for knobs that cannot change its
result. This script instead defines one `PhaseGrid` per phase, each pairing
the phase's *content* (which batch_sizes/strategies to exercise — the other
phases are left empty so run_battery.py skips them) with only the runtime
knobs that phase actually depends on.

For every grid point the script:
  1. loads `battery_config.json`
  2. overwrites it with the phase's content, input pattern, and knob values
  3. saves it back to `battery_config.json`
  4. invokes `python tests/battery/run_battery.py`

By default, every selected phase is run once for each input pattern:
`sinusoidal` and `flat`. Pattern IDs are included in scenario and battery
names to keep their output files separate.

On Ctrl+C, the newly created folder for the interrupted run is removed unless
its final result files show that the run completed. Earlier completed folders
are left untouched.

Usage (from the repository root):
    python tests/battery/multiple_runs.py
    python tests/battery/multiple_runs.py --phase dp online offline   # subset
    python tests/battery/multiple_runs.py --input-pattern flat
"""
from __future__ import annotations

import argparse
import json
import shutil
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
    # "batch_sizes": [1,4,6,8,22],
    "batch_sizes": [4,],
    # "infeasibility_modes": ["min_error_greedy", "carryover"],
    "infeasibility_modes": ["carryover"],
    "online_strategies": [],
    "additional_strategies": [],
}

SWARM_ONLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": ["bandit", "ant_colony"],
    "online_batch_sizes": [8,96,256,1024],
    "additional_strategies": [],
}

PER_REQUEST_ONLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": [
        "singleton_best",
        "random_flavour",
        "random_slot",
        "random_full",
    ],
    # The online runner forces N=1 for these request-level policies.
    "additional_strategies": [],
}

OFFLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "infeasibility_modes": [],
    "online_strategies": [],
    "additional_strategies": [
        "offline_singleton_best",
        "offline_singleton_best_accurate",
        "random",
        "bandit",
        "ant_colony",
    ],
}

INPUT_PATTERNS = ("flat",)

# ─── phase-specific knob grids ─────────────────────────────────────────────────
# DP + singleton_best: rollback x parallelism (swarm_mode is irrelevant to
# both, so it is left fixed at whatever battery_config.json already has).
def build_rollback_parallelism_grid(
    rollbacks: tuple,
    parallelisms: tuple,
    thresholds: tuple,
) -> List[Dict[str, Any]]:
    """Build the rollback x parallelism x threshold grid for the phases that
    are sensitive to rollback (dp / singleton_best).

    `rollback_max_consecutive > 0` only means something when there's more
    than one parallel batch solver to roll back *among* — with
    `max_batch_solver_parallelism == 1` there's nothing to roll back
    against. So:

      - If 0 is one of the requested rollback values, any run with
        parallelism == 1 and rollback > 0 is skipped (it would be redundant
        with the rollback == 0 / parallelism == 1 run that's already in the
        grid).
      - If every requested rollback value is > 0 (no 0 present in
        `rollbacks`), there's no rollback == 0 baseline to fall back on, so
        the parallelism == 1 run is still executed for each rollback value —
        but with an extra `FORCE_ROLLB_PAR1: True` flag set on that grid
        point, flagging that parallelism == 1 is being forced together with
        rollback > 0 on purpose.
    """
    has_zero_rollback = 0 in rollbacks
    grid: List[Dict[str, Any]] = []
    for rollback in rollbacks:
        for parallelism in parallelisms:
            for threshold in thresholds:
                if has_zero_rollback and parallelism == 1 and rollback > 0:
                    continue
                point: Dict[str, Any] = {
                    "rollback_max_consecutive": rollback,
                    "max_batch_solver_parallelism": parallelism,
                    "max_error_threshold": threshold,
                }
                if not has_zero_rollback and parallelism == 1:
                    point["FORCE_ROLLB_PAR1"] = True
                grid.append(point)
    return grid


# for rollback in (0, 4)
# for parallelism in (1, 8, 20)
# for max_error_threshold in (3.0, 3.5, 4.0, 4.5)
_ROLLBACK_X_PARALLELISM_X_THRESHOLD = build_rollback_parallelism_grid(
    rollbacks=(0,),
    parallelisms=(12,),
    thresholds=(4.0,),
)

# bandit/ant_colony: parallelism x swarm_mode x threshold (rollback is irrelevant, left
# fixed).
_PARALLELISM_X_SWARM_MODE = [
    {
        "max_batch_solver_parallelism": parallelism,
        "online_swarm_mode": swarm_mode,
        "max_error_threshold": threshold,
    }
    # for parallelism in (1, 8, 20)
    for parallelism in (12,)
    # for swarm_mode in ("serialized", "merge")
    for swarm_mode in ("merge",)
    for threshold in (4.0,)
]

PHASES: Dict[str, PhaseGrid] = {
    "dp": PhaseGrid("dp", DP_CONTENT, _ROLLBACK_X_PARALLELISM_X_THRESHOLD),
    "online": PhaseGrid("online", SWARM_ONLINE_CONTENT, _PARALLELISM_X_SWARM_MODE),
    "per_request": PhaseGrid(
        "per_request", PER_REQUEST_ONLINE_CONTENT, _ROLLBACK_X_PARALLELISM_X_THRESHOLD
    ),
    # Offline strategies can also be evaluated with different per-scenario error
    # thresholds, even though they don't depend on rollback or swarm knobs.
    "offline": PhaseGrid(
        "offline",
        OFFLINE_CONTENT,
        [{"max_error_threshold": threshold} for threshold in (4.0,)],
    ),
}

# Which phases to run when `--phase` is not given.
DEFAULT_PHASES = ["dp", "online", "per_request", "offline"]


def load_config(path: Path) -> Dict[str, Any]:
    """Load the battery configuration JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path: Path, cfg: Dict[str, Any]) -> None:
    """Save the battery configuration JSON preserving a readable format."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _with_input_pattern(cfg: Dict[str, Any], input_pattern: str) -> Dict[str, Any]:
    """Return a config copy whose scenarios identify one simulation pattern."""
    if input_pattern not in INPUT_PATTERNS:
        raise ValueError(f"Unknown input pattern: {input_pattern!r}")

    patterned_cfg = dict(cfg)
    patterned_cfg["scenarios"] = [
        {
            **scenario,
            "id": f"{scenario['id']}_{input_pattern}",
            "input_pattern": input_pattern,
        }
        for scenario in cfg.get("scenarios", [])
    ]
    return patterned_cfg


def run_battery() -> int:
    """Invoke run_battery.py from the repository root."""
    cmd = [sys.executable, str(RUN_BATTERY_SCRIPT.relative_to(REPO_ROOT))]
    print(f"Running: {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=REPO_ROOT)


def _run_output_root(cfg: Dict[str, Any]) -> Path:
    output_root = Path(cfg.get("output_dir", "results"))
    if not output_root.is_absolute():
        output_root = REPO_ROOT / output_root
    return output_root.resolve()


def _matching_run_directories(
    output_root: Path, battery_id: str
) -> set[Path]:
    prefix = f"{battery_id}_"
    if not output_root.is_dir():
        return set()
    return {
        run_dir.resolve()
        for run_dir in output_root.iterdir()
        if run_dir.name.startswith(prefix)
        and run_dir.is_dir()
        and not run_dir.is_symlink()
    }


def _run_folder_is_complete(run_dir: Path, battery_id: str) -> bool:
    required_files = (
        f"battery_results_{battery_id}.csv",
        f"battery_timings_{battery_id}.csv",
        f"per_n_timings_{battery_id}.csv",
        "README.md",
    )
    return all((run_dir / filename).is_file() for filename in required_files)


def _remove_interrupted_run_folder(
    cfg: Dict[str, Any], existing_run_directories: set[Path]
) -> None:
    """Remove newly created incomplete outputs, but preserve earlier or finished runs."""
    output_root = _run_output_root(cfg)
    battery_id = str(cfg.get("battery_id", "run"))
    if not output_root.is_dir():
        return

    for run_dir in output_root.iterdir():
        if (
            not run_dir.name.startswith(f"{battery_id}_")
            or not run_dir.is_dir()
            or run_dir.is_symlink()
            or run_dir.resolve() in existing_run_directories
            or _run_folder_is_complete(run_dir, battery_id)
        ):
            continue

        print(
            f"Removing incomplete battery run folder after Ctrl+C: {run_dir}",
            file=sys.stderr,
        )
        shutil.rmtree(run_dir)


def run_phase(
    base_cfg: Dict[str, Any],
    phase: PhaseGrid,
    battery_id_prefix: str,
    input_pattern: str,
) -> int:
    """Run every grid point of one phase, returning the last non-zero exit
    status seen (0 if all grid points succeeded)."""
    overall_status = 0
    total = len(phase.knob_grid)

    for idx, knobs in enumerate(phase.knob_grid, start=1):
        cfg = dict(base_cfg)
        cfg.update(phase.content)
        cfg = _with_input_pattern(cfg, input_pattern)

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

        cfg["battery_id"] = (
            f"{battery_id_prefix}_{input_pattern}_{phase.name}_{idx - 1}"
        )

        print("\n" + "=" * 70)
        print(
            f"Pattern '{input_pattern}', phase '{phase.name}' — "
            f"run {idx}/{total}: knobs={knobs}"
        )
        print("=" * 70)

        save_config(CONFIG_PATH, cfg)
        existing_run_directories = _matching_run_directories(
            _run_output_root(cfg), str(cfg.get("battery_id", "run"))
        )
        try:
            status = run_battery()
        except KeyboardInterrupt:
            _remove_interrupted_run_folder(cfg, existing_run_directories)
            raise
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
        default="cfg_test16",
        help="Prefix used to build each run's battery_id "
        "(final id is '<prefix>_<pattern>_<phase>_<grid-index>').",
    )
    parser.add_argument(
        "--input-pattern",
        nargs="+",
        choices=INPUT_PATTERNS,
        default=list(INPUT_PATTERNS),
        help="Simulation request pattern(s) to sweep (default: sinusoidal and flat).",
    )
    args = parser.parse_args(argv)

    if not CONFIG_PATH.exists():
        print(f"Configuration file not found: {CONFIG_PATH}", file=sys.stderr)
        return 1

    base_cfg = load_config(CONFIG_PATH)
    overall_status = 0
    try:
        for input_pattern in args.input_pattern:
            for phase_name in args.phase:
                status = run_phase(
                    base_cfg,
                    PHASES[phase_name],
                    args.battery_id_prefix,
                    input_pattern,
                )
                if status != 0:
                    overall_status = status
    finally:
        # Always restore the on-disk config to its pre-run state so repeated
        # invocations (or manual edits) aren't clobbered by the last grid
        # point's overrides.
        save_config(CONFIG_PATH, base_cfg)

    print("\n" + "=" * 70)
    print(
        f"All requested patterns ({', '.join(args.input_pattern)}) and "
        f"phases ({', '.join(args.phase)}) completed."
    )
    print("=" * 70)
    return overall_status


if __name__ == "__main__":
    sys.exit(main())
