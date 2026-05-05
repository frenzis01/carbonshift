"""Deterministic, synchronous simulator for multi-N Online2 benchmarks."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import config
from scheduler import BatchScheduler
from shared_state import Assignment, Request, SharedSchedulerState

from tests.Nshift_speed.metrics import min_max_avg


@dataclass
class SimulationResult:
    summary: Dict[str, Any]
    per_request: List[Dict[str, Any]]
    per_timeslot: List[Dict[str, Any]]
    batch_timings: List[Dict[str, Any]]


def _patch_online2_config(scenario: Dict[str, Any], batch_size: int, output_csv_path: str) -> Dict[str, Any]:
    metadata = scenario["metadata"]
    original = {
        "BATCH_SIZE": config.BATCH_SIZE,
        "TOTAL_SLOTS": config.TOTAL_SLOTS,
        "SLOT_DURATION_SECONDS": config.SLOT_DURATION_SECONDS,
        "PREDICTED_REQUESTS_PER_SLOT": config.PREDICTED_REQUESTS_PER_SLOT,
        "REQUESTS_PER_SLOT": config.REQUESTS_PER_SLOT,
        "REQUEST_RATE_STD_FACTOR": config.REQUEST_RATE_STD_FACTOR,
        "ERROR_WINDOW_PAST": config.ERROR_WINDOW_PAST,
        "ERROR_WINDOW_FUTURE": config.ERROR_WINDOW_FUTURE,
        "ASSIGNMENT_MAX_FUTURE_SLOTS": config.ASSIGNMENT_MAX_FUTURE_SLOTS,
        "MAX_ERROR_THRESHOLD": config.MAX_ERROR_THRESHOLD,
        "PREHISTORY_USE_VIRTUAL_PAST": config.PREHISTORY_USE_VIRTUAL_PAST,
        "ENABLE_SOLVER_LOGGING": config.ENABLE_SOLVER_LOGGING,
        "ENABLE_INFEASIBILITY_DEBUG_LOGGING": config.ENABLE_INFEASIBILITY_DEBUG_LOGGING,
        "VERBOSE": config.VERBOSE,
        "OUTPUT_FILE": config.OUTPUT_FILE,
    }

    config.BATCH_SIZE = int(batch_size)
    config.TOTAL_SLOTS = int(metadata["total_slots"])
    config.SLOT_DURATION_SECONDS = float(metadata["slot_duration_seconds"])
    config.PREDICTED_REQUESTS_PER_SLOT = float(metadata["requests_per_slot"])
    config.REQUESTS_PER_SLOT = config.PREDICTED_REQUESTS_PER_SLOT
    config.REQUEST_RATE_STD_FACTOR = float(metadata["request_rate_std_factor"])
    config.ERROR_WINDOW_PAST = int(metadata["error_window_past"])
    config.ERROR_WINDOW_FUTURE = int(metadata["error_window_future"])
    config.ASSIGNMENT_MAX_FUTURE_SLOTS = int(metadata["error_window_future"])
    config.MAX_ERROR_THRESHOLD = float(metadata["max_error_threshold"])
    config.PREHISTORY_USE_VIRTUAL_PAST = False
    config.ENABLE_SOLVER_LOGGING = False
    config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = False
    config.VERBOSE = False
    config.OUTPUT_FILE = output_csv_path

    return original


def _restore_online2_config(original: Dict[str, Any]) -> None:
    for key, value in original.items():
        setattr(config, key, value)


def _build_prehistory_assignments(prehistory_slots: List[Dict[str, Any]]) -> Tuple[List[Assignment], Set[int]]:
    assignments: List[Assignment] = []
    synthetic_ids: Set[int] = set()
    synthetic_request_id = -1
    for slot_info in prehistory_slots:
        slot = int(slot_info["slot"])
        count = int(slot_info["request_count"])
        error = float(slot_info["error_per_request"])
        for _ in range(max(0, count)):
            assignments.append(
                Assignment(
                    request_id=synthetic_request_id,
                    scheduled_slot=slot,
                    strategy_name="SyntheticPrehistory",
                    carbon_cost=0.0,
                    error=error,
                    strategy_duration=0,
                    arrival_slot=slot,
                    deadline_slot=slot,
                )
            )
            synthetic_ids.add(synthetic_request_id)
            synthetic_request_id -= 1
    return assignments, synthetic_ids


def _compute_window_rows(
    *,
    total_slots: int,
    window_past: int,
    window_future: int,
    real_assignments: List[Assignment],
    modeled_assignments: List[Assignment],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for center in range(total_slots):
        start = center - window_past
        end = center + window_future
        real_errors = [a.error for a in real_assignments if start <= a.scheduled_slot <= end]
        modeled_errors = [a.error for a in modeled_assignments if start <= a.scheduled_slot <= end]
        rows.append(
            {
                "timeslot": center,
                "window_start": start,
                "window_end": end,
                "real_request_count": len(real_errors),
                "modeled_request_count": len(modeled_errors),
                "window_avg_error_real": (sum(real_errors) / len(real_errors)) if real_errors else 0.0,
                "window_avg_error_modeled": (sum(modeled_errors) / len(modeled_errors)) if modeled_errors else 0.0,
            }
        )
    return rows


def run_single_batch_size(
    scenario: Dict[str, Any],
    batch_size: int,
    *,
    flush_partial_batch: bool = True,
    output_csv_path: str = "/tmp/nshift_dummy_assignments.csv",
) -> SimulationResult:
    """
    Run one deterministic benchmark simulation for a specific batch size.

    This uses BatchScheduler internals synchronously (no threads) so the same
    static scenario can be replayed across multiple batch sizes.
    """
    output_csv = Path(output_csv_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    original_cfg = _patch_online2_config(scenario, batch_size, str(output_csv))
    try:
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)
        scheduler.carbon_forecast = [float(v) for v in scenario["carbon_forecast"]]
        scheduler.dp_solver.carbon_forecast = list(scheduler.carbon_forecast)
        scheduler.dp_solver.window_size = int(scenario["metadata"]["total_slots"])

        prehistory_assignments, synthetic_ids = _build_prehistory_assignments(scenario.get("prehistory_slots", []))
        if prehistory_assignments:
            shared_state.add_assignments(prehistory_assignments)

        requests_by_slot: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        request_rows: Dict[int, Dict[str, Any]] = {}
        for request in scenario["requests"]:
            request_id = int(request["request_id"])
            request_obj = {
                "request_id": request_id,
                "arrival_slot": int(request["arrival_slot"]),
                "arrival_time": float(request["arrival_time"]),
                "deadline_slot": int(request["deadline_slot"]),
            }
            requests_by_slot[request_obj["arrival_slot"]].append(request_obj)
            request_rows[request_id] = request_obj

        inclusion_slot: Dict[int, int] = {}
        inclusion_batch_sequence: Dict[int, int] = {}
        inclusion_solver_seconds: Dict[int, float] = {}

        batch_sequence = 0
        batch_timings: List[Dict[str, Any]] = []
        total_slots = int(scenario["metadata"]["total_slots"])
        slot_duration = float(scenario["metadata"]["slot_duration_seconds"])

        for slot in range(total_slots):
            shared_state.set_current_slot(slot)
            for request_data in requests_by_slot.get(slot, []):
                shared_state.add_request(
                    Request(
                        id=request_data["request_id"],
                        arrival_slot=request_data["arrival_slot"],
                        deadline_slot=request_data["deadline_slot"],
                    )
                )

            while shared_state.get_pending_count() >= batch_size:
                pending_before = shared_state.get_pending_requests(batch_size)
                pending_ids = [int(req.id) for req in pending_before]
                for request_id in pending_ids:
                    inclusion_slot.setdefault(request_id, slot)

                t0 = time.perf_counter()
                scheduled = scheduler._process_batch(slot)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0

                batch_sequence += 1
                batch_timings.append(
                    {
                        "batch_sequence": batch_sequence,
                        "batch_size_n": int(batch_size),
                        "effective_batch_size": int(batch_size),
                        "slot": int(slot),
                        "pending_before": len(pending_ids),
                        "solver_elapsed_ms": float(elapsed_ms),
                        "scheduled": bool(scheduled),
                        "flush_partial_batch": False,
                    }
                )

                for request_id in pending_ids:
                    inclusion_batch_sequence.setdefault(request_id, batch_sequence)
                    inclusion_solver_seconds.setdefault(request_id, elapsed_ms / 1000.0)

                if not scheduled:
                    break

        if flush_partial_batch:
            flush_slot = max(0, total_slots - 1)
            while shared_state.get_pending_count() > 0:
                pending_count = shared_state.get_pending_count()
                effective_batch = min(batch_size, pending_count)
                pending_before = shared_state.get_pending_requests(effective_batch)
                pending_ids = [int(req.id) for req in pending_before]
                for request_id in pending_ids:
                    inclusion_slot.setdefault(request_id, flush_slot)

                original_batch_size = int(config.BATCH_SIZE)
                config.BATCH_SIZE = effective_batch
                try:
                    t0 = time.perf_counter()
                    scheduled = scheduler._process_batch(flush_slot)
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                finally:
                    config.BATCH_SIZE = original_batch_size

                batch_sequence += 1
                batch_timings.append(
                    {
                        "batch_sequence": batch_sequence,
                        "batch_size_n": int(batch_size),
                        "effective_batch_size": int(effective_batch),
                        "slot": int(flush_slot),
                        "pending_before": len(pending_ids),
                        "solver_elapsed_ms": float(elapsed_ms),
                        "scheduled": bool(scheduled),
                        "flush_partial_batch": effective_batch != batch_size,
                    }
                )

                for request_id in pending_ids:
                    inclusion_batch_sequence.setdefault(request_id, batch_sequence)
                    inclusion_solver_seconds.setdefault(request_id, elapsed_ms / 1000.0)

                if not scheduled:
                    break

        all_assignments = shared_state.get_current_assignments()
        real_assignments = [a for rid, a in all_assignments.items() if int(rid) not in synthetic_ids]
        modeled_assignments = list(real_assignments) + prehistory_assignments

        per_request_rows: List[Dict[str, Any]] = []
        queue_wait_samples: List[float] = []
        final_wait_samples: List[float] = []

        for request_id in sorted(request_rows.keys()):
            request_info = request_rows[request_id]
            assignment = all_assignments.get(request_id)
            included_slot = inclusion_slot.get(request_id)
            batch_seq = inclusion_batch_sequence.get(request_id)
            scheduling_sec = inclusion_solver_seconds.get(request_id, 0.0)

            if included_slot is None:
                queue_wait_seconds = None
                queue_wait_slots = None
            else:
                queue_wait_slots = int(included_slot - request_info["arrival_slot"])
                queue_wait_seconds = float(queue_wait_slots * slot_duration)
                queue_wait_samples.append(queue_wait_seconds)

            if assignment is None:
                scheduled_slot = None
                strategy_name = ""
                error = None
                carbon_cost = None
                final_wait_slots = None
                final_wait_seconds = None
            else:
                scheduled_slot = int(assignment.scheduled_slot)
                strategy_name = str(assignment.strategy_name)
                error = float(assignment.error)
                carbon_cost = float(assignment.carbon_cost)
                final_wait_slots = int(scheduled_slot - request_info["arrival_slot"])
                final_wait_seconds = float(final_wait_slots * slot_duration + scheduling_sec)
                final_wait_samples.append(final_wait_seconds)

            per_request_rows.append(
                {
                    "request_id": request_id,
                    "arrival_time": float(request_info["arrival_time"]),
                    "arrival_slot": int(request_info["arrival_slot"]),
                    "deadline_slot": int(request_info["deadline_slot"]),
                    "included_in_batch_slot": included_slot if included_slot is not None else "",
                    "batch_sequence": batch_seq if batch_seq is not None else "",
                    "scheduled_slot": scheduled_slot if scheduled_slot is not None else "",
                    "queue_wait_slots": queue_wait_slots if queue_wait_slots is not None else "",
                    "queue_wait_seconds": queue_wait_seconds if queue_wait_seconds is not None else "",
                    "final_wait_slots": final_wait_slots if final_wait_slots is not None else "",
                    "final_wait_seconds": final_wait_seconds if final_wait_seconds is not None else "",
                    "strategy_name": strategy_name,
                    "error": error if error is not None else "",
                    "carbon_cost": carbon_cost if carbon_cost is not None else "",
                }
            )

        window_rows = _compute_window_rows(
            total_slots=total_slots,
            window_past=int(scenario["metadata"]["error_window_past"]),
            window_future=int(scenario["metadata"]["error_window_future"]),
            real_assignments=real_assignments,
            modeled_assignments=modeled_assignments,
        )

        solver_samples = [
            float(row["solver_elapsed_ms"])
            for row in batch_timings
            if bool(row["scheduled"])
        ]
        solver_stats = min_max_avg(solver_samples)
        queue_stats = min_max_avg(queue_wait_samples)
        final_stats = min_max_avg(final_wait_samples)

        total_carbon_cost = sum(float(a.carbon_cost) for a in real_assignments)
        global_average_error = (
            sum(float(a.error) for a in real_assignments) / len(real_assignments)
            if real_assignments
            else 0.0
        )

        summary = {
            "batch_size": int(batch_size),
            "requests_total": len(request_rows),
            "requests_scheduled": len(real_assignments),
            "requests_unscheduled": len(request_rows) - len(real_assignments),
            "batches_executed": len(batch_timings),
            "solver_time_ms_min": solver_stats["min"],
            "solver_time_ms_max": solver_stats["max"],
            "solver_time_ms_avg": solver_stats["avg"],
            "queue_wait_seconds_min": queue_stats["min"],
            "queue_wait_seconds_max": queue_stats["max"],
            "queue_wait_seconds_avg": queue_stats["avg"],
            "final_wait_seconds_min": final_stats["min"],
            "final_wait_seconds_max": final_stats["max"],
            "final_wait_seconds_avg": final_stats["avg"],
            "total_carbon_cost": float(total_carbon_cost),
            "global_average_error": float(global_average_error),
        }

        return SimulationResult(
            summary=summary,
            per_request=per_request_rows,
            per_timeslot=window_rows,
            batch_timings=batch_timings,
        )
    finally:
        _restore_online2_config(original_cfg)
