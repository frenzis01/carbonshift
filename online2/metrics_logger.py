"""
Structured CSV logging for Online2 solver executions.
"""

import csv
import os
import threading
import time
from typing import Dict, List, Optional


class SolverMetricsLogger:
    """Thread-safe CSV logger for solver run metrics."""

    RUN_FIELDS = [
        "run_id",
        "run_sequence",
        "current_slot",
        "pending_batch_size",
        "total_assignments",
        "new_assignments",
        "replanned_assignments",
        "solver_status",
        "solver_mode",
        "lock_future_assignments",
        "solver_start_ts",
        "solver_end_ts",
        "solver_elapsed_ms",
        "avg_ms_per_new_request",
        "avg_ms_per_assignment",
        "total_carbon_cost",
        "carbon_cost_per_new_request",
        "carbon_cost_per_assignment",
        "error_window_avg_after",
        "error_window_avg_after_real",
        "error_window_start_slot",
        "error_window_end_slot",
        "error_window_threshold",
        "error_window_violated_after",
        "error_window_violated_after_real",
        "batches_processed_after",
        "total_scheduled_after",
    ]

    ASSIGNMENT_FIELDS = [
        "run_id",
        "current_slot",
        "solver_start_ts",
        "solver_end_ts",
        "request_id",
        "is_pending_request",
        "is_new_assignment_in_run",
        "scheduled_slot",
        "strategy_name",
        "strategy_duration",
        "error",
        "carbon_cost",
        "arrival_slot",
        "deadline_slot",
    ]

    SLOT_METRIC_FIELDS = [
        "run_id",
        "current_slot",
        "scheduled_slot",
        "run_slot_count",
        "total_slot_count_after",
        "avg_error_in_slot",
        "run_avg_error_in_slot",
        "slot_has_assignments_after",
        "carbon_intensity",
        "capacity_multiplier_after",
        "capacity_level_max_requests",
        "request_ids",
        "strategy_breakdown",
    ]

    INFEASIBLE_DEBUG_FIELDS = [
        "event_id",
        "timestamp",
        "current_slot",
        "pending_batch_size",
        "pending_request_details",
        "strict_threshold",
        "baseline_error_sum",
        "baseline_request_count",
        "baseline_average_error",
        "min_strategy_error",
        "max_strategy_error",
        "min_possible_avg_error_pending_only",
        "max_possible_avg_error_pending_only",
        "strict_infeasible_by_error_bound",
        "strict_scheduled_pending_count",
        "relaxed_scheduled_pending_count",
        "relaxed_success",
        "lock_future_assignments",
        "future_assignment_count",
        "future_slot_counts",
        "future_assignment_details",
        "all_active_slot_counts",
    ]

    def __init__(
        self,
        enabled: bool,
        runs_file: str,
        assignments_file: str,
        slot_metrics_file: str,
        infeasible_debug_file: Optional[str] = None,
    ) -> None:
        self.enabled = enabled
        self.runs_file = runs_file
        self.assignments_file = assignments_file
        self.slot_metrics_file = slot_metrics_file
        self.infeasible_debug_file = infeasible_debug_file
        self._lock = threading.Lock()

        if self.enabled:
            self._ensure_file_with_header(self.runs_file, self.RUN_FIELDS)
            self._ensure_file_with_header(self.assignments_file, self.ASSIGNMENT_FIELDS)
            self._ensure_file_with_header(self.slot_metrics_file, self.SLOT_METRIC_FIELDS)
            if self.infeasible_debug_file:
                self._ensure_file_with_header(self.infeasible_debug_file, self.INFEASIBLE_DEBUG_FIELDS)

    def log_solver_run(
        self,
        run_data: Dict,
        assignment_rows: Optional[List[Dict]] = None,
        slot_metric_rows: Optional[List[Dict]] = None,
    ) -> str:
        """
        Persist one solver run and its detailed rows.

        Returns:
            run_id used for this execution.
        """
        if not self.enabled:
            return ""

        assignment_rows = assignment_rows or []
        slot_metric_rows = slot_metric_rows or []

        run_id = run_data.get("run_id")
        if not run_id:
            run_id = f"{int(time.time() * 1000)}-{run_data.get('current_slot', 0)}"
            run_data["run_id"] = run_id

        with self._lock:
            self._append_row(self.runs_file, self.RUN_FIELDS, run_data)
            if assignment_rows:
                for row in assignment_rows:
                    row["run_id"] = run_id
                self._append_rows(self.assignments_file, self.ASSIGNMENT_FIELDS, assignment_rows)
            if slot_metric_rows:
                for row in slot_metric_rows:
                    row["run_id"] = run_id
                self._append_rows(self.slot_metrics_file, self.SLOT_METRIC_FIELDS, slot_metric_rows)
        return run_id

    def log_infeasible_debug(self, event_data: Dict) -> str:
        """
        Persist one strict-infeasibility debug event.

        Returns:
            event_id used for this debug row.
        """
        if not self.enabled or not self.infeasible_debug_file:
            return ""

        event_id = event_data.get("event_id")
        if not event_id:
            event_id = f"{int(time.time() * 1000)}-{event_data.get('current_slot', 0)}"
            event_data["event_id"] = event_id
        if "timestamp" not in event_data:
            event_data["timestamp"] = time.time()

        with self._lock:
            self._append_row(self.infeasible_debug_file, self.INFEASIBLE_DEBUG_FIELDS, event_data)
        return event_id

    def _ensure_file_with_header(self, path: str, header: List[str]) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        must_create = not os.path.exists(path) or os.path.getsize(path) == 0
        if not must_create:
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                existing_header = next(reader, [])
            must_create = existing_header != header
            if must_create:
                backup_path = f"{path}.bak.{int(time.time())}"
                os.replace(path, backup_path)

        if must_create:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=header)
                writer.writeheader()

    def _append_row(self, path: str, header: List[str], row: Dict) -> None:
        self._append_rows(path, header, [row])

    def _append_rows(self, path: str, header: List[str], rows: List[Dict]) -> None:
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            for row in rows:
                normalized = {k: row.get(k, "") for k in header}
                writer.writerow(normalized)
