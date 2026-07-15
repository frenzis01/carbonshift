
#!/usr/bin/env python3
"""
Run `run_battery.py` across the parameter grid defined by the battery table.

For every row of the table the script:
  1. loads `battery_config.json`
  2. updates (in place, without adding duplicates) the three runtime knobs:
     - rollback_max_consecutive
     - max_batch_solver_parallelism
     - online_swarm_mode
  3. saves the configuration back to `battery_config.json`
  4. invokes `python tests/battery/run_battery.py`

Usage (from the repository root):
    python tests/battery/multiple_runs.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

# ─── paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
BATTERY_DIR = REPO_ROOT / "tests" / "battery"
CONFIG_PATH = BATTERY_DIR / "battery_config.json"
RUN_BATTERY_SCRIPT = BATTERY_DIR / "run_battery.py"

# ─── parameter grid from the pinned table ─────────────────────────────────────
PARAM_GRID = [
    # rollback_max_consecutive, max_batch_solver_parallelism, online_swarm_mode
    (0, 1, "serialized"),
    (0, 1, "merge"),
    (0, 8, "serialized"),
    (0, 8, "merge"),
    (0, 20, "serialized"),
    (0, 20, "merge"),
    (4, 1, "serialized"),
    (4, 1, "merge"),
    (4, 8, "serialized"),
    (4, 8, "merge"),
    (4, 20, "serialized"),
    (4, 20, "merge"),
]


def load_config(path: Path) -> Dict[str, Any]:
    """Load the battery configuration JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path: Path, cfg: Dict[str, Any]) -> None:
    """Save the battery configuration JSON preserving a readable format."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def update_runtime_knobs(
    cfg: Dict[str, Any],
    rollback: int,
    parallelism: int,
    swarm_mode: str,
) -> None:
    """Update the three runtime knobs in the existing dictionary.

    Keys are overwritten in place; if they did not exist they are added once.
    """
    cfg["rollback_max_consecutive"] = rollback
    cfg["max_batch_solver_parallelism"] = parallelism
    cfg["online_swarm_mode"] = swarm_mode


def run_battery() -> int:
    """Invoke run_battery.py from the repository root."""
    cmd = [sys.executable, str(RUN_BATTERY_SCRIPT.relative_to(REPO_ROOT))]
    print(f"Running: {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=REPO_ROOT)


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"Configuration file not found: {CONFIG_PATH}", file=sys.stderr)
        return 1

    overall_status = 0
    total = len(PARAM_GRID)

    for idx, (rollback, parallelism, swarm_mode) in enumerate(PARAM_GRID, start=1):
        print("\n" + "=" * 70)
        print(
            f"Run {idx}/{total}: "
            f"rollback={rollback}, parallelism={parallelism}, mode={swarm_mode}"
        )
        print("=" * 70)

        cfg = load_config(CONFIG_PATH)
        update_runtime_knobs(cfg, rollback, parallelism, swarm_mode)
        cfg["battery_id"] = f"cfg{idx - 1}"
        save_config(CONFIG_PATH, cfg)

        status = run_battery()
        if status != 0:
            print(
                f"WARNING: run_battery.py exited with status {status} "
                f"for run {idx}",
                file=sys.stderr,
            )
            overall_status = status

    print("\n" + "=" * 70)
    print(f"All {total} runs completed.")
    print("=" * 70)
    return overall_status


if __name__ == "__main__":
    sys.exit(main())

