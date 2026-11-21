#!/usr/bin/env python3
"""
Run `run_battery.py` across several PHASE-AWARE parameter grids.

Each phase of the Rust battery harness (see run_battery.py's module docstring)
is sensitive to a different subset of runtime knobs:

  - Every online strategy is sensitive to `max_batch_solver_parallelism`.
  - The `online` phase is the joint-batch ACO sweep.
  - The `per_request` phase keeps Bandit, singleton-best, Hazard, and the two
    random-reference policies grouped by their request-level decision policy,
    while sweeping their batch size.
    A batch still processes requests sequentially where the algorithm does so;
    it changes how often scheduler setup and committed feedback occur.
  - offline strategies (offline_singleton_best, offline_singleton_best_accurate,
    hazard, bandit, ant_colony): run each scenario synchronously, without the
    online worker pool, and are insensitive to online worker parallelism.

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

By default, every selected phase is run once with the `sinusoidal` input
pattern, the configured carbon relation, and the configured request-wave
orientation. Edit `CARBON_REQUEST_ALIGNMENT`, `CARBON_PHASE_SHIFT_FRACTION`,
and `REQUEST_PATTERN_START` beside `INPUT_PATTERNS` to change those settings.
`--input-pattern flat`
selects the flat pattern instead; the two input patterns can also be requested
together. Pattern settings are included in scenario and battery names to keep
their output files separate.

Edit the `FLAVOURS` list beside `INPUT_PATTERNS` to choose the flavour
definitions shared by the baseline and all enabled phases. Comment out entries
to test a subset. This list overrides `battery_config.json`'s `flavours` field
for runs launched by this script.

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
import os
import re
import signal
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

sys.path.insert(0, str(BATTERY_DIR / "scenarios"))
from scenario_io import CARBON_REQUEST_ALIGNMENTS, REQUEST_PATTERN_STARTS  # noqa: E402


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
    # "batch_sizes": [6, 8, 10, 12, 16, 22],
    # "batch_sizes": [8,10,22],
    # "batch_sizes": [1,4,6,8,22],
    "batch_sizes": [1,],
    "online_strategies": [],
    "additional_strategies": [],
}

SWARM_ONLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "online_strategies": ["ant_colony"],
    "online_batch_sizes": [8],
    "additional_strategies": [],
}

PER_REQUEST_ONLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "online_strategies": [
        "bandit",
        "singleton_best",
        "hazard",
        "random_slotflav",
        "random_flav",
    ],
    "online_batch_sizes": [8],
    "additional_strategies": [],
}

OFFLINE_CONTENT: Dict[str, Any] = {
    "batch_sizes": [],
    "online_strategies": [],
    "additional_strategies": [
        "offline_singleton_best",
        "offline_singleton_best_accurate",
        "offline_random_slotflav",
        "offline_random_flav",
        "hazard",
        "bandit",
        "ant_colony",
    ],
}

SUPPORTED_INPUT_PATTERNS = ("sinusoidal", "flat")
OUTPUT_INPUT_PATTERN_TOKENS = {"sinusoidal": "sine", "flat": "FLAT"}
OUTPUT_ALIGNMENT_TOKENS = {
    "in-phase": "IP",
    "phase-opposition": "PO",
    "phase-shifted": "PS",
}
OUTPUT_REQUEST_START_TOKENS = {"low": "L", "high": "H"}
OUTPUT_PHASE_TOKENS = {"per_request": "perrequest"}
_SCENARIO_WAVE_SUFFIX = re.compile(
    r"_(?:IP|PO)_[LH]$|_PS_shift[+-]?[0-9]+(?:p[0-9]+)?(?:e[+-]?[0-9]+)?_[LH]$"
)

# Shared by the baseline and all phases. Comment out entries to restrict the
# available solver choices for a test run.
FLAVOURS: List[Dict[str, Any]] = [
    {"name": "Accurate", "error": 0.0, "duration_sec": 60.0},
    {"name": "Balanced", "error": 2.5, "duration_sec": 30.0},
    {"name": "Fast", "error": 5.0, "duration_sec": 10.0},
]

# TODO: better assess
APPEND_SUFFIX_TO_SCENARIOS = True

BATTERY_ID_PREFIX = "T40"

# Select which supported scenario pattern(s) the default sweep runs.
INPUT_PATTERNS = ("sinusoidal",)
# Sinusoidal-only relationship, shift amount, and request-wave orientation.
# "in-phase"
# "phase_opposition"
# "phase-shifted"
CARBON_REQUEST_ALIGNMENT = "in-phase"
# Used only for phase-shifted; 0.25 is halfway from alignment to opposition.
CARBON_PHASE_SHIFT_FRACTION = 0.25
REQUEST_PATTERN_START = "low"

# ─── phase-specific knob grids ─────────────────────────────────────────────────
# DP + online strategies: parallelism x threshold.
# for parallelism in (1, 8, 20)
# for max_error_threshold in (3.0, 3.5, 4.0, 4.5)
_PARALLELISM_X_THRESHOLD = [
    {
        "max_batch_solver_parallelism": parallelism,
        "max_error_threshold": threshold,
    }
    for parallelism in (1,)
    for threshold in (0.0,0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)
]

PHASES: Dict[str, PhaseGrid] = {
    "dp": PhaseGrid("dp", DP_CONTENT, _PARALLELISM_X_THRESHOLD),
    "online": PhaseGrid("online", SWARM_ONLINE_CONTENT, _PARALLELISM_X_THRESHOLD),
    "per_request": PhaseGrid(
        "per_request", PER_REQUEST_ONLINE_CONTENT, _PARALLELISM_X_THRESHOLD
    ),
    # Offline strategies can also be evaluated with different per-scenario error
    # thresholds; their execution is independent of the online worker pool.
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


def _strip_scenario_output_suffixes(scenario_id: str) -> str:
    """Remove pattern and wave tags previously added by this sweep script."""
    while True:
        previous = scenario_id
        scenario_id = _SCENARIO_WAVE_SUFFIX.sub("", scenario_id)
        for token in OUTPUT_INPUT_PATTERN_TOKENS.values():
            suffix = f"_{token}"
            if scenario_id.endswith(suffix):
                scenario_id = scenario_id[: -len(suffix)]
        if scenario_id == previous:
            return scenario_id


def _with_input_pattern(cfg: Dict[str, Any], input_pattern: str) -> Dict[str, Any]:
    """Return a config copy whose scenarios identify one simulation pattern."""
    if input_pattern not in SUPPORTED_INPUT_PATTERNS:
        raise ValueError(f"Unknown input pattern: {input_pattern!r}")

    output_token = OUTPUT_INPUT_PATTERN_TOKENS[input_pattern]
    patterned_cfg = dict(cfg)
    patterned_cfg["scenarios"] = []
    for scenario in cfg.get("scenarios", []):
        scenario_id = _strip_scenario_output_suffixes(scenario["id"])
        if APPEND_SUFFIX_TO_SCENARIOS:
            scenario_id = f"{scenario_id}_{output_token}"
        patterned_cfg["scenarios"].append(
            {**scenario, "id": scenario_id, "input_pattern": input_pattern}
        )
    return patterned_cfg


def _wave_output_suffix() -> str:
    """Return compact output-only tags for the selected sinusoidal wave."""
    alignment = OUTPUT_ALIGNMENT_TOKENS[CARBON_REQUEST_ALIGNMENT]
    request_start = OUTPUT_REQUEST_START_TOKENS[REQUEST_PATTERN_START]
    if CARBON_REQUEST_ALIGNMENT == "phase-shifted":
        shift = f"{CARBON_PHASE_SHIFT_FRACTION:g}".replace(".", "p")
        alignment = f"{alignment}_shift{shift}"
    return f"_{alignment}_{request_start}"


def _with_sinusoidal_wave_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if CARBON_REQUEST_ALIGNMENT not in CARBON_REQUEST_ALIGNMENTS:
        raise ValueError(f"Unknown carbon request alignment: {CARBON_REQUEST_ALIGNMENT!r}")
    if REQUEST_PATTERN_START not in REQUEST_PATTERN_STARTS:
        raise ValueError(f"Unknown request pattern start: {REQUEST_PATTERN_START!r}")
    if not 0.0 <= CARBON_PHASE_SHIFT_FRACTION <= 0.5:
        raise ValueError("CARBON_PHASE_SHIFT_FRACTION must be between 0 and 0.5")

    wave_suffix = _wave_output_suffix()
    configured = dict(cfg)
    configured["scenarios"] = []
    for scenario in cfg.get("scenarios", []):
        if scenario.get("input_pattern") != "sinusoidal":
            configured["scenarios"].append(scenario)
            continue

        scenario_id = _strip_scenario_output_suffixes(scenario["id"])
        if APPEND_SUFFIX_TO_SCENARIOS:
            scenario_id = (
                f"{scenario_id}_"
                f"{OUTPUT_INPUT_PATTERN_TOKENS['sinusoidal']}{wave_suffix}"
            )
        configured["scenarios"].append(
            {
                **scenario,
                "id": scenario_id,
                "carbon_request_alignment": CARBON_REQUEST_ALIGNMENT,
                "carbon_phase_shift_fraction": CARBON_PHASE_SHIFT_FRACTION,
                "request_pattern_start": REQUEST_PATTERN_START,
            }
        )
    return configured


def run_battery() -> int:
    """Invoke run_battery.py, stopping its process tree cleanly on Ctrl+C."""
    cmd = [sys.executable, str(RUN_BATTERY_SCRIPT.relative_to(REPO_ROOT))]
    print(f"Running: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        # Isolate the child tree so Ctrl+C can be forwarded once, then fully
        # waited for before the caller removes an incomplete output directory.
        start_new_session=(os.name == "posix"),
    )
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGINT)
                elif hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        raise


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
        cfg["flavours"] = FLAVOURS
        cfg = _with_input_pattern(cfg, input_pattern)
        if input_pattern == "sinusoidal":
            cfg = _with_sinusoidal_wave_settings(cfg)

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

        input_pattern_token = OUTPUT_INPUT_PATTERN_TOKENS[input_pattern]
        wave_suffix = (
            _wave_output_suffix()
            if input_pattern == "sinusoidal"
            else ""
        )
        phase_token = OUTPUT_PHASE_TOKENS.get(phase.name, phase.name)
        cfg["battery_id"] = (
            f"{battery_id_prefix}_{input_pattern_token}{wave_suffix}_{phase_token}_{idx - 1}"
        )

        print("\n" + "=" * 70)
        wave_settings = (
            f"carbon_alignment={CARBON_REQUEST_ALIGNMENT}, "
            f"phase_shift={CARBON_PHASE_SHIFT_FRACTION:g}, "
            f"request_start={REQUEST_PATTERN_START}"
            if input_pattern == "sinusoidal"
            else "wave settings=n/a"
        )
        print(
            f"Pattern '{input_pattern}', phase '{phase.name}' — "
            f"{wave_settings}, "
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
        default=BATTERY_ID_PREFIX,
        help="Prefix used to build each run's battery_id "
        "(final id includes input/wave settings, phase, and grid index).",
    )
    parser.add_argument(
        "--input-pattern",
        nargs="+",
        choices=SUPPORTED_INPUT_PATTERNS,
        default=list(INPUT_PATTERNS),
        help="Simulation request pattern(s) to sweep (default: sinusoidal).",
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
    except KeyboardInterrupt:
        print(
            "\nInterrupted by user. The active child process was stopped, "
            "incomplete output was cleaned, and the original battery config "
            "was restored.",
            file=sys.stderr,
        )
        overall_status = 130
    finally:
        # Always restore the on-disk config to its pre-run state so repeated
        # invocations (or manual edits) aren't clobbered by the last grid
        # point's overrides.
        save_config(CONFIG_PATH, base_cfg)

    print("\n" + "=" * 70)
    print(
        f"All requested patterns ({', '.join(args.input_pattern)}) and "
        f"phases ({', '.join(args.phase)}) completed with sinusoidal alignment "
        f"{CARBON_REQUEST_ALIGNMENT} "
        f"{f'(shift={CARBON_PHASE_SHIFT_FRACTION:g}) ' if CARBON_REQUEST_ALIGNMENT == 'phase-shift' else ''}"
        f"and request start {REQUEST_PATTERN_START}."
    )
    print("=" * 70)
    return overall_status


if __name__ == "__main__":
    sys.exit(main())
