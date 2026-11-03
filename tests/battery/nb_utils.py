"""Shared utilities for online2 analysis notebooks."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

import pandas as pd


FLAVOUR_COLORS: Dict[str, str] = {
    "Fast": "#1f77b4",
    "Balanced": "#2ca02c",
    "Accurate": "#ff7f0e",
}


@dataclass(frozen=True)
class AssignmentRun:
    """Describe one assignment result set, regardless of its on-disk layout."""

    assignment_dir: Path
    scenario_dir: Path
    run_folder_id: str | None
    run_type: str
    mode_label: str
    batch_sizes: tuple[int, ...] = ()


def discover_assignment_runs(
    results_root: Path, run_folder_filter: str | None = None
) -> list[AssignmentRun]:
    """Find batch and offline assignment results in the battery output tree.

    Batch runs store ``per_request.csv`` under ``rust_<mode>/N<batch_size>``;
    offline strategies store it under ``rust_offline/strategy_<name>``.
    """
    results_root = Path(results_root)
    search_root = results_root / run_folder_filter if run_folder_filter else results_root
    batch_sizes_by_mode: dict[Path, set[int]] = {}
    offline_dirs: set[Path] = set()

    for per_request_path in search_root.rglob("per_request.csv"):
        assignment_dir = per_request_path.parent
        parent = assignment_dir.parent
        if (
            assignment_dir.name.startswith("N")
            and assignment_dir.name[1:].isdigit()
            and parent.name.startswith("rust_")
            and parent.name != "rust_offline"
        ):
            batch_sizes_by_mode.setdefault(parent, set()).add(int(assignment_dir.name[1:]))
        elif (
            assignment_dir.name.startswith("strategy_")
            and parent.name == "rust_offline"
        ):
            offline_dirs.add(assignment_dir)

    runs = []
    for mode_dir, batch_sizes in batch_sizes_by_mode.items():
        scenario_dir = mode_dir.parent
        relative_parts = scenario_dir.relative_to(results_root).parts
        runs.append(
            AssignmentRun(
                assignment_dir=mode_dir,
                scenario_dir=scenario_dir,
                run_folder_id=relative_parts[0] if len(relative_parts) > 1 else None,
                run_type="batch",
                mode_label=mode_dir.name.removeprefix("rust_"),
                batch_sizes=tuple(sorted(batch_sizes)),
            )
        )

    for assignment_dir in offline_dirs:
        scenario_dir = assignment_dir.parent.parent
        relative_parts = scenario_dir.relative_to(results_root).parts
        strategy_name = assignment_dir.name.removeprefix("strategy_")
        runs.append(
            AssignmentRun(
                assignment_dir=assignment_dir,
                scenario_dir=scenario_dir,
                run_folder_id=relative_parts[0] if len(relative_parts) > 1 else None,
                run_type="offline",
                mode_label=f"offline_{strategy_name}",
            )
        )

    return sorted(
        runs,
        key=lambda run: (run.assignment_dir.stat().st_mtime, str(run.assignment_dir)),
        reverse=True,
    )


def bool_series(values: pd.Series) -> pd.Series:
    """Coerce a column of various truthy representations to a boolean Series."""
    return values.astype(str).str.strip().str.lower().isin(["1", "true", "yes", "y", "t"])


def resolve_plot_mode(mode: str) -> str:
    """Validate and normalise a timeline highlight plot mode string."""
    normalized = str(mode).strip().lower()
    if normalized not in {"scatter", "stacked", "both"}:
        raise ValueError("TIMELINE_HIGHLIGHT_PLOT_MODE must be one of: scatter, stacked, both")
    return normalized


def prepare_local(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce and sort an assignment timeline DataFrame by arrival_slot / request_id."""
    local = df.copy()
    for col in ("request_id", "arrival_slot", "scheduled_slot"):
        local[col] = pd.to_numeric(local[col], errors="coerce")
    local = local.dropna(subset=["request_id", "arrival_slot", "scheduled_slot"]).copy()
    local["request_id"] = local["request_id"].astype(int)
    local["arrival_slot"] = local["arrival_slot"].astype(int)
    local["scheduled_slot"] = local["scheduled_slot"].astype(int)
    local["flavour_name"] = local.get("flavour_name", "Unknown").fillna("Unknown").astype(str)
    return local.sort_values(["arrival_slot", "request_id"]).reset_index(drop=True)


def flavour_order(local: pd.DataFrame) -> list:
    """Return flavour names ordered by FLAVOUR_COLORS key order, then alphabetically."""
    seen = set(local["flavour_name"].unique().tolist())
    ordered = [name for name in FLAVOUR_COLORS if name in seen]
    ordered.extend(sorted(name for name in seen if name not in ordered))
    return ordered

