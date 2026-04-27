"""
Utilities to visualize Online2 solver logs with matplotlib.
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime

import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import pandas as pd

import config


def load_solver_logs(
    runs_file: str = config.SOLVER_RUNS_FILE,
    assignments_file: str = config.SOLVER_ASSIGNMENTS_FILE,
    slot_metrics_file: str = config.SOLVER_SLOT_METRICS_FILE,
    only_runs_with_assignments: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load solver log CSVs into dataframes."""
    runs_df = pd.read_csv(runs_file) if _file_exists(runs_file) else pd.DataFrame()
    assignments_df = pd.read_csv(assignments_file) if _file_exists(assignments_file) else pd.DataFrame()
    slot_metrics_df = pd.read_csv(slot_metrics_file) if _file_exists(slot_metrics_file) else pd.DataFrame()

    if only_runs_with_assignments and not runs_df.empty and not assignments_df.empty:
        assignment_run_ids = set(assignments_df["run_id"].dropna().astype(str).tolist())
        runs_df = runs_df[runs_df["run_id"].astype(str).isin(assignment_run_ids)].copy()
        if not slot_metrics_df.empty:
            slot_metrics_df = slot_metrics_df[
                slot_metrics_df["run_id"].astype(str).isin(assignment_run_ids)
            ].copy()

    return runs_df, assignments_df, slot_metrics_df


def load_infeasibility_debug_logs(
    debug_file: str = config.SOLVER_INFEASIBLE_DEBUG_FILE,
) -> pd.DataFrame:
    """Load strict-infeasibility debug CSV."""
    return pd.read_csv(debug_file) if _file_exists(debug_file) else pd.DataFrame()


def select_run_ids(
    runs_df: pd.DataFrame,
    mode: str = "all",
    target_slot: Optional[int] = None,
) -> List[str]:
    """
    Select run IDs for plotting.

    mode:
      - "all": all runs in time order
      - "first_per_slot": one run (earliest) for each current_slot
      - "last_per_slot": one run (latest) for each current_slot
      - "all_for_slot": all runs for target_slot
    """
    if runs_df.empty:
        return []

    df = runs_df.sort_values("solver_start_ts").copy()
    mode = mode.strip().lower()

    if mode == "all":
        return df["run_id"].astype(str).tolist()

    if mode == "first_per_slot":
        return (
            df.groupby("current_slot", sort=True, as_index=False)
            .first()["run_id"]
            .astype(str)
            .tolist()
        )

    if mode == "last_per_slot":
        return (
            df.groupby("current_slot", sort=True, as_index=False)
            .last()["run_id"]
            .astype(str)
            .tolist()
        )

    if mode == "all_for_slot":
        if target_slot is None:
            return []
        return df[df["current_slot"] == int(target_slot)]["run_id"].astype(str).tolist()

    raise ValueError(f"Unknown run selection mode: {mode}")


def plot_processing_times(runs_df: pd.DataFrame):
    """
    Single figure with:
    - average processing time per request
    - processing time per batch
    """
    if runs_df.empty:
        raise ValueError("runs_df is empty: no solver run data to plot.")

    df = runs_df.sort_values("solver_start_ts").reset_index(drop=True)
    x = df.index + 1

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(x, df["avg_ms_per_new_request"], marker="o", linewidth=2, label="Avg ms / request")
    ax.plot(x, df["solver_elapsed_ms"], marker="s", linewidth=2, label="Batch solve time (ms)")
    ax.set_xlabel("Solver execution #")
    ax.set_ylabel("Milliseconds")
    ax.set_title("Solver processing time trend")
    ax.grid(True, alpha=0.25)
    ax.legend()
    plt.tight_layout()
    return fig


def plot_solver_execution_stacked(
    run_id: str,
    runs_df: pd.DataFrame,
    assignments_df: pd.DataFrame,
    slot_metrics_df: Optional[pd.DataFrame] = None,
    strategy_colors: Optional[Dict[str, str]] = None,
):
    """
    Stacked bar chart for one solver execution:
    - bars by scheduled slot
    - stack by strategy
    - request IDs annotated
    - previous assignments faded, new ones emphasized
    - avg slot error line
    - capacity level horizontal lines
    """
    if strategy_colors is None:
        strategy_colors = {
            "Fast": "#1f77b4",
            "Balanced": "#2ca02c",
            "Accurate": "#ff7f0e",
        }

    run_id = str(run_id)
    run_row = runs_df[runs_df["run_id"].astype(str) == run_id]
    if run_row.empty:
        raise ValueError(f"Missing run data for run_id={run_id}")
    run_assignments = assignments_df[assignments_df["run_id"].astype(str) == run_id].copy()

    run_info = run_row.iloc[0]
    current_slot = int(run_info["current_slot"])
    start_dt = datetime.fromtimestamp(float(run_info["solver_start_ts"]))
    end_dt = datetime.fromtimestamp(float(run_info["solver_end_ts"]))
    all_slots = list(range(int(config.TOTAL_SLOTS)))
    slots = all_slots

    fig, ax = plt.subplots(figsize=(13, 6))

    run_slots = pd.DataFrame()
    if slot_metrics_df is not None and not slot_metrics_df.empty:
        run_slots = slot_metrics_df[slot_metrics_df["run_id"].astype(str) == run_id].copy()
        if not run_slots.empty:
            run_slots["scheduled_slot"] = run_slots["scheduled_slot"].astype(int)
            run_slots = run_slots.set_index("scheduled_slot").reindex(all_slots).reset_index()

    if not run_slots.empty:
        total_counts = run_slots["total_slot_count_after"].fillna(0.0).tolist()
    else:
        total_counts = (
            run_assignments.groupby("scheduled_slot")["request_id"]
            .count()
            .reindex(all_slots, fill_value=0)
            .tolist()
            if not run_assignments.empty
            else [0] * len(all_slots)
        )

    ax.bar(
        slots,
        total_counts,
        label="Total assigned after run",
        color="#d9d9d9",
        alpha=0.35,
        width=0.85,
        edgecolor="black",
        linewidth=0.25,
        zorder=1,
    )

    if not run_assignments.empty:
        run_assignments["scheduled_slot"] = pd.to_numeric(run_assignments["scheduled_slot"], errors="coerce")
        run_assignments["request_id"] = pd.to_numeric(run_assignments["request_id"], errors="coerce")
        run_assignments = run_assignments.dropna(subset=["scheduled_slot", "request_id"])
        run_assignments["scheduled_slot"] = run_assignments["scheduled_slot"].astype(int)
        run_assignments["request_id"] = run_assignments["request_id"].astype(int)
        if "is_new_assignment_in_run" in run_assignments.columns:
            run_assignments["is_new_assignment_in_run"] = run_assignments["is_new_assignment_in_run"].apply(
                _as_bool
            )
        else:
            run_assignments["is_new_assignment_in_run"] = True

        run_assignments = run_assignments.sort_values(["scheduled_slot", "strategy_name", "request_id"])

        slot_stack_height = {slot: 0 for slot in slots}
        strategy_with_label = set()
        shown_new_legend = False
        shown_old_legend = False

        for row in run_assignments.itertuples(index=False):
            slot = int(row.scheduled_slot)
            strategy = str(row.strategy_name)
            color = strategy_colors.get(strategy, "#7f7f7f")
            bottom = slot_stack_height.get(slot, 0)
            is_new = bool(row.is_new_assignment_in_run)

            label = "_nolegend_"
            if strategy not in strategy_with_label and is_new:
                label = strategy
                strategy_with_label.add(strategy)
            elif is_new and not shown_new_legend:
                label = "New in this run"
                shown_new_legend = True
            elif (not is_new) and not shown_old_legend:
                label = "Previous assignment (faded)"
                shown_old_legend = True

            alpha = 0.95 if is_new else 0.28

            ax.bar(
                [slot],
                [1.0],
                bottom=[bottom],
                width=0.72,
                color=color,
                alpha=alpha,
                edgecolor="black",
                linewidth=0.35,
                label=label,
                zorder=3 if is_new else 2,
            )
            ax.text(
                slot,
                bottom + 0.5,
                f"{int(row.request_id)}",
                ha="center",
                va="center",
                fontsize=7,
                color="white" if is_new else "black",
                path_effects=[pe.withStroke(linewidth=1.1, foreground="black")] if is_new else [],
                zorder=4,
            )
            slot_stack_height[slot] = bottom + 1
    else:
        ax.text(
            0.5,
            0.95,
            "No assignments for this solver execution",
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=10,
            color="dimgray",
        )

    if not run_slots.empty:
        ax2 = ax.twinx()
        if "slot_has_assignments_after" in run_slots.columns:
            raw_has_data = run_slots["slot_has_assignments_after"].fillna(False)
            has_data = raw_has_data.apply(_as_bool)
        else:
            has_data = run_slots["total_slot_count_after"].fillna(0) > 0

        error_col = "avg_error_in_slot"
        avg_errors = pd.to_numeric(run_slots[error_col], errors="coerce").where(has_data)
        ax2.plot(
            slots,
            avg_errors,
            color="black",
            marker="o",
            linewidth=1.2,
            linestyle="-",
            label="Avg error per slot (total after run)",
        )
        ax2.set_ylabel("Average error (%)")
        ax2.legend(loc="upper right")

    for tier in config.CAPACITY_TIERS:
        max_req = tier["max_requests"]
        if max_req == float("inf"):
            continue
        ax.axhline(
            y=max_req,
            linestyle="--",
            linewidth=0.8,
            alpha=0.4,
            color="gray",
        )
        ax.text(
            max(slots) + 0.3 if slots else 0.3,
            max_req,
            f"cap≤{max_req} (x{tier['multiplier']})",
            fontsize=8,
            color="gray",
            va="bottom",
        )

    ax.axvline(current_slot, linestyle=":", color="red", alpha=0.7, label=f"Current slot={current_slot}")
    ax.set_xlim(-0.6, config.TOTAL_SLOTS - 0.4)
    tick_step = max(1, config.TOTAL_SLOTS // 12)
    ax.set_xticks(list(range(0, config.TOTAL_SLOTS, tick_step)))
    ax.set_xlabel("Scheduled slot")
    ax.set_ylabel("Assignments count")
    ax.set_title(
        f"Solver run {run_id}\n"
        f"start={start_dt.strftime('%H:%M:%S.%f')[:-3]} "
        f"end={end_dt.strftime('%H:%M:%S.%f')[:-3]} "
        f"elapsed={float(run_info['solver_elapsed_ms']):.2f} ms"
    )
    ax.legend(loc="upper left")
    ax.grid(True, axis="y", alpha=0.2)
    plt.tight_layout()
    return fig


def plot_infeasibility_overview(debug_df: pd.DataFrame):
    """
    Overview of strict-infeasible events:
    - count by current slot
    - min possible avg error vs threshold
    """
    if debug_df.empty:
        raise ValueError("debug_df is empty: no strict-infeasible events.")

    df = debug_df.copy()
    df["current_slot"] = pd.to_numeric(df["current_slot"], errors="coerce")
    df["min_possible_avg_error_pending_only"] = pd.to_numeric(
        df["min_possible_avg_error_pending_only"], errors="coerce"
    )
    df["strict_threshold"] = pd.to_numeric(df["strict_threshold"], errors="coerce")
    df = df.dropna(subset=["current_slot"])

    slot_counts = df.groupby("current_slot").size()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=False)

    ax1.bar(slot_counts.index.astype(int), slot_counts.values, color="#8da0cb", edgecolor="black", linewidth=0.4)
    ax1.set_title("Strict-infeasible events per current slot")
    ax1.set_xlabel("Current slot")
    ax1.set_ylabel("Event count")
    ax1.grid(True, axis="y", alpha=0.2)

    x = range(1, len(df) + 1)
    ax2.plot(
        x,
        df["min_possible_avg_error_pending_only"].tolist(),
        marker="o",
        linewidth=1.5,
        label="Min possible avg error (pending only)",
    )
    ax2.plot(
        x,
        df["strict_threshold"].tolist(),
        linestyle="--",
        linewidth=1.5,
        label="Strict threshold",
    )
    ax2.set_title("Strict infeasibility severity over events")
    ax2.set_xlabel("Event #")
    ax2.set_ylabel("Error (%)")
    ax2.grid(True, alpha=0.2)
    ax2.legend()

    plt.tight_layout()
    return fig


def plot_infeasibility_event(event_id: str, debug_df: pd.DataFrame):
    """
    Detail plot for one strict-infeasible event.
    Shows:
    - current active/future slot load
    - pending requests and deadlines
    - summary metrics (threshold/baseline bounds)
    """
    if debug_df.empty:
        raise ValueError("debug_df is empty: no strict-infeasible events.")

    event_id = str(event_id)
    row_df = debug_df[debug_df["event_id"].astype(str) == event_id]
    if row_df.empty:
        raise ValueError(f"Missing strict-infeasible event: {event_id}")
    row = row_df.iloc[0]

    active_counts = _parse_int_map(row.get("all_active_slot_counts", ""))
    future_counts = _parse_int_map(row.get("future_slot_counts", ""))
    pending_details = _parse_pending_details(row.get("pending_request_details", ""))
    current_slot = int(float(row.get("current_slot", 0)))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=False)

    slots = list(range(int(config.TOTAL_SLOTS)))
    active_series = [active_counts.get(slot, 0) for slot in slots]
    future_series = [future_counts.get(slot, 0) for slot in slots]
    ax1.bar(slots, active_series, color="#bdbdbd", alpha=0.45, edgecolor="black", linewidth=0.35, label="All active")
    ax1.bar(
        slots,
        future_series,
        color="#3182bd",
        alpha=0.65,
        edgecolor="black",
        linewidth=0.35,
        label="Future baseline (slot>=current)",
    )
    ax1.axvline(current_slot, color="red", linestyle=":", linewidth=1.0, label=f"Current slot={current_slot}")
    ax1.set_title(f"Strict infeasibility event {event_id}: slot loads snapshot")
    ax1.set_xlabel("Slot")
    ax1.set_ylabel("Assigned requests")
    ax1.grid(True, axis="y", alpha=0.2)
    ax1.legend()

    if pending_details:
        pending_ids = [rid for rid, _ in pending_details]
        deadlines = [dl for _, dl in pending_details]
        y = list(range(len(pending_ids)))
        ax2.scatter(deadlines, y, color="#e6550d", zorder=3)
        for i, (rid, ddl) in enumerate(pending_details):
            ax2.text(ddl, i, f" r{rid}", va="center", ha="left", fontsize=9)
        ax2.axvline(current_slot, color="red", linestyle=":", linewidth=1.0)
        ax2.set_yticks(y)
        ax2.set_yticklabels([str(rid) for rid in pending_ids])
        ax2.set_xlabel("Deadline slot")
        ax2.set_ylabel("Pending request id")
        ax2.set_title("Pending requests (strict-fail attempt)")
        ax2.grid(True, alpha=0.2)
    else:
        ax2.text(0.5, 0.5, "No pending details available", transform=ax2.transAxes, ha="center", va="center")
        ax2.axis("off")

    summary = (
        f"baseline_avg={float(row.get('baseline_average_error', 0.0)):.2f}%  "
        f"threshold={float(row.get('strict_threshold', 0.0)):.2f}%  "
        f"min_possible_avg={float(row.get('min_possible_avg_error_pending_only', 0.0)):.2f}%  "
        f"strict_covered={int(float(row.get('strict_scheduled_pending_count', 0)))} / "
        f"{int(float(row.get('pending_batch_size', 0)))}  "
        f"relaxed_covered={int(float(row.get('relaxed_scheduled_pending_count', 0)))}"
    )
    fig.text(0.01, 0.01, summary, fontsize=9, ha="left", va="bottom")

    plt.tight_layout()
    return fig


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _parse_int_map(serialized: str) -> Dict[int, int]:
    result: Dict[int, int] = {}
    if not isinstance(serialized, str) or not serialized.strip():
        return result
    for item in serialized.split("|"):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        try:
            result[int(k)] = int(v)
        except ValueError:
            continue
    return result


def _parse_pending_details(serialized: str) -> List[Tuple[int, int]]:
    details: List[Tuple[int, int]] = []
    if not isinstance(serialized, str) or not serialized.strip():
        return details
    for item in serialized.split("|"):
        if ":" not in item:
            continue
        rid, ddl = item.split(":", 1)
        try:
            details.append((int(rid), int(ddl)))
        except ValueError:
            continue
    return details


def _file_exists(path: str) -> bool:
    try:
        with open(path, "r"):
            return True
    except OSError:
        return False
