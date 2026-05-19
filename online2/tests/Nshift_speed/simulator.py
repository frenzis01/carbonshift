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


def _is_greedy_fallback_mode(mode: str) -> bool:
    return str(mode).strip().lower() in {"greedy_after_infeasible", "greedy_fallback"}


def _is_relaxed_retry_mode(mode: str) -> bool:
    return str(mode).strip().lower().startswith("dp_relaxed")


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
    window_past_decay_slots: int,
    real_assignments: List[Assignment],
    modeled_assignments: List[Assignment],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for center in range(total_slots):
        start = center - window_past
        end = center + window_future
        real_errors = [a.error for a in real_assignments if start <= a.scheduled_slot <= end]
        modeled_errors = [a.error for a in modeled_assignments if start <= a.scheduled_slot <= end]

        real_error_sum = float(sum(real_errors))
        real_count = float(len(real_errors))
        modeled_error_sum = float(sum(modeled_errors))
        modeled_count = float(len(modeled_errors))

        for offset in range(1, max(0, int(window_past_decay_slots)) + 1):
            decay_slot = int(start) - offset
            decay_weight = float(window_past_decay_slots - offset + 1) / float(window_past_decay_slots + 1)

            slot_real = [a.error for a in real_assignments if a.scheduled_slot == decay_slot]
            if slot_real:
                avg_real = float(sum(slot_real)) / float(len(slot_real))
                weighted_real_count = float(len(slot_real)) * decay_weight
                real_count += weighted_real_count
                real_error_sum += avg_real * weighted_real_count

            slot_modeled = [a.error for a in modeled_assignments if a.scheduled_slot == decay_slot]
            if slot_modeled:
                avg_modeled = float(sum(slot_modeled)) / float(len(slot_modeled))
                weighted_modeled_count = float(len(slot_modeled)) * decay_weight
                modeled_count += weighted_modeled_count
                modeled_error_sum += avg_modeled * weighted_modeled_count
        rows.append(
            {
                "timeslot": center,
                "window_start": start,
                "window_end": end,
                "real_request_count": real_count,
                "modeled_request_count": modeled_count,
                "window_avg_error_real": (real_error_sum / real_count) if real_count > 0.0 else 0.0,
                "window_avg_error_modeled": (modeled_error_sum / modeled_count) if modeled_count > 0.0 else 0.0,
            }
        )
    return rows


def _select_greedy_baseline_strategy() -> Tuple[str, int]:
    strategies = list(getattr(config, "STRATEGIES", []))
    if not strategies:
        return "Accurate", 0
    selected = min(
        strategies,
        key=lambda strategy: (
            float(strategy.get("error", 0.0)),
            -int(strategy.get("duration", 0)),
        ),
    )
    return str(selected.get("name", "Accurate")), int(selected.get("duration", 0))


def _get_capacity_multiplier(capacity_tiers: List[Dict[str, Any]], request_count: int) -> float:
    for tier in capacity_tiers:
        if request_count <= float(tier["max_requests"]):
            return float(tier["multiplier"])
    return float(capacity_tiers[-1]["multiplier"])


def _incremental_carbon_cost(
    *,
    slot_carbon: float,
    add_duration: int,
    before_count: int,
    before_duration: int,
    capacity_tiers: List[Dict[str, Any]],
) -> float:
    after_count = before_count + 1
    after_duration = before_duration + add_duration
    before_mult = _get_capacity_multiplier(capacity_tiers, before_count)
    after_mult = _get_capacity_multiplier(capacity_tiers, after_count)
    before_cost = slot_carbon * before_mult * before_duration
    after_cost = slot_carbon * after_mult * after_duration
    return after_cost - before_cost


def run_single_batch_size(
    scenario: Dict[str, Any],
    batch_size: int,
    *,
    flush_partial_batch: bool = True,
    realtime_slots: bool = False,
    realtime_speed_scale: float = 1.0,
    output_csv_path: str = "/tmp/nshift_dummy_assignments.csv",
    skip_first_k_slots: int = 0,
) -> SimulationResult:
    """
    Run one deterministic benchmark simulation for a specific batch size.

    This uses BatchScheduler internals synchronously (no threads) so the same
    static scenario can be replayed across multiple batch sizes.
    """
    if not (0.0 <= float(realtime_speed_scale) <= 1.0):
        raise ValueError("realtime_speed_scale must be in [0.0, 1.0].")

    output_csv = Path(output_csv_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    original_cfg = _patch_online2_config(scenario, batch_size, str(output_csv))
    try:
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)
        scheduler.carbon_forecast = [float(v) for v in scenario["carbon_forecast"]]
        scheduler.dp_solver.carbon_forecast = list(scheduler.carbon_forecast)
        scheduler.dp_solver.window_size = int(scenario["metadata"]["total_slots"])

        request_assignment_mode: Dict[int, str] = {}
        request_assignment_status: Dict[int, str] = {}
        original_solve_dp = scheduler._solve_dp

        def _solve_dp_with_trace(
            pending: List[Request],
            current_slot: int,
        ) -> Tuple[List[Assignment], Dict[str, Any]]:
            assignments, solve_context = original_solve_dp(pending, current_slot)
            mode = str(solve_context.get("mode", ""))
            status = str(solve_context.get("status", ""))
            pending_ids = {int(req.id) for req in pending}
            for assignment in assignments:
                request_id = int(assignment.request_id)
                if request_id in pending_ids:
                    request_assignment_mode[request_id] = mode
                    request_assignment_status[request_id] = status
            return assignments, solve_context

        scheduler._solve_dp = _solve_dp_with_trace

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
        elapsed_slot_seconds = (
            slot_duration * float(realtime_speed_scale) if realtime_slots else slot_duration
        )
        realtime_t0 = time.monotonic() if realtime_slots else 0.0

        for slot in range(total_slots):
            if realtime_slots:
                target_time = realtime_t0 + (slot * elapsed_slot_seconds)
                remaining = target_time - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)

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
                queue_wait_seconds = float(queue_wait_slots * elapsed_slot_seconds)
                queue_wait_samples.append(queue_wait_seconds)

            if assignment is None:
                scheduled_slot = None
                strategy_name = ""
                error = None
                carbon_cost = None
                final_wait_slots = None
                final_wait_seconds = None
                assignment_mode = ""
                assignment_status = ""
                assigned_with_greedy_fallback = False
                assigned_with_relaxed_retry = False
            else:
                scheduled_slot = int(assignment.scheduled_slot)
                strategy_name = str(assignment.strategy_name)
                error = float(assignment.error)
                carbon_cost = float(assignment.carbon_cost)
                final_wait_slots = int(scheduled_slot - request_info["arrival_slot"])
                final_wait_seconds = float(final_wait_slots * elapsed_slot_seconds + scheduling_sec)
                final_wait_samples.append(final_wait_seconds)
                assignment_mode = request_assignment_mode.get(request_id, "")
                assignment_status = request_assignment_status.get(request_id, "")
                assigned_with_greedy_fallback = _is_greedy_fallback_mode(assignment_mode)
                assigned_with_relaxed_retry = _is_relaxed_retry_mode(assignment_mode)

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
                    "assignment_solver_mode": assignment_mode,
                    "assignment_solver_status": assignment_status,
                    "assigned_with_greedy_fallback": assigned_with_greedy_fallback,
                    "assigned_with_relaxed_retry": assigned_with_relaxed_retry,
                }
            )

        window_rows = _compute_window_rows(
            total_slots=total_slots,
            window_past=int(scenario["metadata"]["error_window_past"]),
            window_future=int(scenario["metadata"]["error_window_future"]),
            window_past_decay_slots=int(
                scenario["metadata"].get(
                    "error_window_past_decay_slots",
                    getattr(config, "ERROR_WINDOW_PAST_DECAY_SLOTS", 0),
                )
            ),
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
        global_average_error_real = (
            sum(float(a.error) for a in real_assignments) / len(real_assignments)
            if real_assignments
            else 0.0
        )
        global_average_error_modeled = (
            sum(float(a.error) for a in modeled_assignments) / len(modeled_assignments)
            if modeled_assignments
            else 0.0
        )
        
        # Compute skip-first-K variant (steady-state, excluding startup transient)
        global_average_error_real_skip_first_k = 0.0
        global_average_error_modeled_skip_first_k = 0.0
        if skip_first_k_slots > 0:
            real_skip = [
                a for a in real_assignments
                if int(a.scheduled_slot) >= skip_first_k_slots
            ]
            modeled_skip = [
                a for a in modeled_assignments
                if int(a.scheduled_slot) >= skip_first_k_slots
            ]
            global_average_error_real_skip_first_k = (
                sum(float(a.error) for a in real_skip) / len(real_skip)
                if real_skip else 0.0
            )
            global_average_error_modeled_skip_first_k = (
                sum(float(a.error) for a in modeled_skip) / len(modeled_skip)
                if modeled_skip else 0.0
            )
        requests_assigned_with_greedy_fallback = sum(
            1
            for row in per_request_rows
            if bool(row.get("assigned_with_greedy_fallback", False))
        )
        requests_assigned_with_relaxed_retry = sum(
            1
            for row in per_request_rows
            if bool(row.get("assigned_with_relaxed_retry", False))
        )

        summary = {
            "execution_mode": "nshift_dp",
            "batch_size": int(batch_size),
            "realtime_slots": bool(realtime_slots),
            "realtime_speed_scale": float(realtime_speed_scale),
            "baseline_strategy_name": "",
            "baseline_strategy_duration": 0,
            "baseline_strategy_error": 0.0,
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
            # Keep global_average_error as modeled (with synthetic prehistory)
            # for direct comparison with modeled window-history charts.
            "global_average_error": float(global_average_error_modeled),
            "global_average_error_real": float(global_average_error_real),
            "global_average_error_modeled": float(global_average_error_modeled),
            "global_average_error_real_skip_first_k": float(global_average_error_real_skip_first_k),
            "global_average_error_modeled_skip_first_k": float(global_average_error_modeled_skip_first_k),
            "requests_assigned_with_greedy_fallback": int(requests_assigned_with_greedy_fallback),
            "requests_assigned_with_relaxed_retry": int(requests_assigned_with_relaxed_retry),
        }

        return SimulationResult(
            summary=summary,
            per_request=per_request_rows,
            per_timeslot=window_rows,
            batch_timings=batch_timings,
        )
    finally:
        _restore_online2_config(original_cfg)


def run_greedy_baseline(
    scenario: Dict[str, Any],
    *,
    realtime_slots: bool = False,
    realtime_speed_scale: float = 1.0,
    skip_first_k_slots: int = 0,
) -> SimulationResult:
    """
    Run immediate per-request baseline without carbon-shifting decisions.

    Each request is scheduled as soon as it arrives (scheduled_slot=arrival_slot),
    with zero modeled error to represent maximum-accuracy processing.
    Carbon cost still applies using Online2 capacity tiers and strategy duration.
    """
    if not (0.0 <= float(realtime_speed_scale) <= 1.0):
        raise ValueError("realtime_speed_scale must be in [0.0, 1.0].")

    metadata = scenario["metadata"]
    total_slots = int(metadata["total_slots"])
    slot_duration = float(metadata["slot_duration_seconds"])
    elapsed_slot_seconds = (
        slot_duration * float(realtime_speed_scale) if realtime_slots else slot_duration
    )
    realtime_t0 = time.monotonic() if realtime_slots else 0.0

    prehistory_assignments, _ = _build_prehistory_assignments(scenario.get("prehistory_slots", []))
    baseline_strategy_name, baseline_strategy_duration = _select_greedy_baseline_strategy()
    baseline_strategy_error = 0.0

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

    slot_counts = [0 for _ in range(total_slots)]
    slot_durations = [0 for _ in range(total_slots)]
    capacity_tiers = list(config.CAPACITY_TIERS)
    carbon_forecast = [float(v) for v in scenario["carbon_forecast"]]

    per_request_rows: List[Dict[str, Any]] = []
    real_assignments: List[Assignment] = []
    batch_timings: List[Dict[str, Any]] = []
    queue_wait_samples: List[float] = []
    final_wait_samples: List[float] = []

    for slot in range(total_slots):
        if realtime_slots:
            target_time = realtime_t0 + (slot * elapsed_slot_seconds)
            remaining = target_time - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

        slot_requests = requests_by_slot.get(slot, [])
        if not slot_requests:
            continue

        for request in slot_requests:
            before_count = slot_counts[slot]
            before_duration = slot_durations[slot]
            delta_cost = _incremental_carbon_cost(
                slot_carbon=carbon_forecast[slot],
                add_duration=baseline_strategy_duration,
                before_count=before_count,
                before_duration=before_duration,
                capacity_tiers=capacity_tiers,
            )
            slot_counts[slot] += 1
            slot_durations[slot] += baseline_strategy_duration

            assignment = Assignment(
                request_id=int(request["request_id"]),
                scheduled_slot=slot,
                strategy_name=baseline_strategy_name,
                carbon_cost=float(delta_cost),
                error=float(baseline_strategy_error),
                strategy_duration=baseline_strategy_duration,
                arrival_slot=int(request["arrival_slot"]),
                deadline_slot=int(request["deadline_slot"]),
            )
            real_assignments.append(assignment)

            queue_wait_samples.append(0.0)
            final_wait_samples.append(0.0)
            per_request_rows.append(
                {
                    "request_id": int(request["request_id"]),
                    "arrival_time": float(request["arrival_time"]),
                    "arrival_slot": int(request["arrival_slot"]),
                    "deadline_slot": int(request["deadline_slot"]),
                    "included_in_batch_slot": slot,
                    "batch_sequence": slot + 1,
                    "scheduled_slot": slot,
                    "queue_wait_slots": 0,
                    "queue_wait_seconds": 0.0,
                    "final_wait_slots": 0,
                    "final_wait_seconds": 0.0,
                    "strategy_name": baseline_strategy_name,
                    "error": float(baseline_strategy_error),
                    "carbon_cost": float(delta_cost),
                    "assignment_solver_mode": "baseline_immediate",
                    "assignment_solver_status": "ok",
                    "assigned_with_greedy_fallback": False,
                    "assigned_with_relaxed_retry": False,
                }
            )

        batch_timings.append(
            {
                "batch_sequence": slot + 1,
                "batch_size_n": 1,
                "effective_batch_size": len(slot_requests),
                "slot": slot,
                "pending_before": len(slot_requests),
                "solver_elapsed_ms": 0.0,
                "scheduled": True,
                "flush_partial_batch": False,
            }
        )

    modeled_assignments = list(real_assignments) + prehistory_assignments
    window_rows = _compute_window_rows(
        total_slots=total_slots,
        window_past=int(metadata["error_window_past"]),
        window_future=int(metadata["error_window_future"]),
        window_past_decay_slots=int(
            metadata.get(
                "error_window_past_decay_slots",
                getattr(config, "ERROR_WINDOW_PAST_DECAY_SLOTS", 0),
            )
        ),
        real_assignments=real_assignments,
        modeled_assignments=modeled_assignments,
    )

    solver_stats = min_max_avg([0.0 for _ in batch_timings])
    queue_stats = min_max_avg(queue_wait_samples)
    final_stats = min_max_avg(final_wait_samples)

    total_carbon_cost = sum(float(a.carbon_cost) for a in real_assignments)
    global_average_error_real = (
        sum(float(a.error) for a in real_assignments) / len(real_assignments)
        if real_assignments
        else 0.0
    )
    global_average_error_modeled = (
        sum(float(a.error) for a in modeled_assignments) / len(modeled_assignments)
        if modeled_assignments
        else 0.0
    )
    
    # Compute skip-first-K variant (steady-state, excluding startup transient)
    global_average_error_real_skip_first_k = 0.0
    global_average_error_modeled_skip_first_k = 0.0
    if skip_first_k_slots > 0:
        real_skip = [
            a for a in real_assignments
            if int(a.scheduled_slot) >= skip_k
        ]
        modeled_skip = [
            a for a in modeled_assignments
            if int(a.scheduled_slot) >= skip_k
        ]
        global_average_error_real_skip_first_k = (
            sum(float(a.error) for a in real_skip) / len(real_skip)
            if real_skip else 0.0
        )
        global_average_error_modeled_skip_first_k = (
            sum(float(a.error) for a in modeled_skip) / len(modeled_skip)
            if modeled_skip else 0.0
        )

    summary = {
        "execution_mode": "greedy_baseline_immediate",
        "batch_size": 0,
        "realtime_slots": bool(realtime_slots),
        "realtime_speed_scale": float(realtime_speed_scale),
        "baseline_strategy_name": baseline_strategy_name,
        "baseline_strategy_duration": int(baseline_strategy_duration),
        "baseline_strategy_error": float(baseline_strategy_error),
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
        "global_average_error": float(global_average_error_modeled),
        "global_average_error_real": float(global_average_error_real),
        "global_average_error_modeled": float(global_average_error_modeled),
        "global_average_error_real_skip_first_k": float(global_average_error_real_skip_first_k),
        "global_average_error_modeled_skip_first_k": float(global_average_error_modeled_skip_first_k),
        "requests_assigned_with_greedy_fallback": 0,
        "requests_assigned_with_relaxed_retry": 0,
    }

    return SimulationResult(
        summary=summary,
        per_request=per_request_rows,
        per_timeslot=window_rows,
        batch_timings=batch_timings,
    )
