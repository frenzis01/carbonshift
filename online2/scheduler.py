"""
Batch Scheduler Module

Consumes pending requests in batches and schedules them using DP with optional Beam Search.
Considers:
- Previous assignments and their effects
- Capacity tiers (rebound effect)
- Sliding error window constraints
"""

import threading
import time
import random
import math
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass
from collections import defaultdict

from shared_state import Request, Assignment, SharedSchedulerState
import config
from metrics_logger import SolverMetricsLogger

# Import DP solver
from rolling_window_dp import RollingWindowDPScheduler


@dataclass
class Strategy:
    """Strategy definition"""
    name: str
    error: float
    duration: int


class BatchScheduler:
    """
    DP-based batch scheduler.
    
    Processes N requests at a time using dynamic programming.
    Considers:
    - Current and historical assignments
    - Capacity tier multipliers (rebound effect)
    - Error budget across sliding window
    """

    def __init__(self, shared_state: SharedSchedulerState):
        """Initialize scheduler"""
        self.shared_state = shared_state
        self.strategies = [Strategy(**s) for s in config.STRATEGIES]
        self.strategy_duration_by_name = {s["name"]: int(s["duration"]) for s in config.STRATEGIES}

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Statistics
        self._batches_processed = 0
        self._total_scheduled = 0
        self._solver_total_time_ms = 0.0
        self._solver_total_runs = 0
        self._solver_total_requests = 0
        self._last_solver_elapsed_ms = 0.0
        self._last_infeasible_slot: Optional[int] = None
        self._last_infeasible_pending: Optional[int] = None

        # Initialize DP solver
        self.carbon_forecast = self._get_carbon_forecast()
        strategies_for_dp = [
            {
                'name': s['name'],
                'error': s['error'],
                'duration': s['duration']
            }
            for s in config.STRATEGIES
        ]
        
        self.dp_solver = RollingWindowDPScheduler(
            strategies=strategies_for_dp,
            carbon_forecast=self.carbon_forecast,
            window_size=config.TOTAL_SLOTS,
            pruning=config.DP_PRUNING_STRATEGY,
            pruning_k=config.DP_PRUNING_K,
            timeout=config.DP_TIMEOUT
        )

        self.metrics_logger = SolverMetricsLogger(
            enabled=config.ENABLE_SOLVER_LOGGING,
            runs_file=config.SOLVER_RUNS_FILE,
            assignments_file=config.SOLVER_ASSIGNMENTS_FILE,
            slot_metrics_file=config.SOLVER_SLOT_METRICS_FILE,
            infeasible_debug_file=(
                config.SOLVER_INFEASIBLE_DEBUG_FILE
                if getattr(config, "ENABLE_INFEASIBILITY_DEBUG_LOGGING", False)
                else None
            ),
        )

    def start(self) -> None:
        """Start scheduler thread"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

        if config.VERBOSE:
            print(f"[Scheduler] Started (batch_size={config.BATCH_SIZE})")

    def stop(self) -> None:
        """Stop scheduler thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)

        if config.VERBOSE:
            print(f"[Scheduler] Stopped (processed {self._batches_processed} batches)")

    def _run(self) -> None:
        """Main scheduler loop (runs in thread)"""
        slot_duration = config.SLOT_DURATION_SECONDS
        slot_start_time = time.time()

        while self._running:
            now = time.time()
            elapsed = now - slot_start_time
            slot = int(elapsed / slot_duration)

            # Update current slot in shared state
            self.shared_state.set_current_slot(slot)

            # Check if we have enough pending requests
            pending_count = self.shared_state.get_pending_count()

            if pending_count >= config.BATCH_SIZE:
                if config.VERBOSE:
                    print(f"\n[Scheduler] Slot {slot}: {pending_count} pending, scheduling batch...")

                # Avoid retry storm: if same slot and same pending count were just
                # infeasible, wait for slot/pending change before retrying.
                if (
                    self._last_infeasible_slot == slot
                    and self._last_infeasible_pending == pending_count
                ):
                    time.sleep(0.1)
                    continue

                scheduled = self._process_batch(slot)
                if scheduled:
                    self._last_infeasible_slot = None
                    self._last_infeasible_pending = None
                else:
                    self._last_infeasible_slot = slot
                    self._last_infeasible_pending = pending_count

            # Small sleep
            time.sleep(0.1)

    def _process_batch(self, current_slot: int) -> bool:
        """
        Process a batch of pending requests.
        
        Args:
            current_slot: Current time slot
        """
        # Get requests to schedule
        pending = self.shared_state.get_pending_requests(config.BATCH_SIZE)

        if not pending:
            return False

        if config.VERBOSE:
            print(f"[Scheduler] Processing {len(pending)} requests...")

        solver_start_wall = time.time()
        solver_start_perf = time.perf_counter()

        # Solve batch scheduling problem using DP
        assignments, solve_context = self._solve_dp(pending, current_slot)

        solver_elapsed_ms = (time.perf_counter() - solver_start_perf) * 1000.0
        solver_end_wall = time.time()
        new_assignments = len(pending)
        total_assignments = len(assignments)
        replanned_assignments = max(0, total_assignments - new_assignments)
        avg_ms_per_new_request = solver_elapsed_ms / new_assignments if new_assignments else 0.0
        avg_ms_per_assignment = solver_elapsed_ms / total_assignments if total_assignments else 0.0

        if assignments:
            total_cost = sum(a.carbon_cost for a in assignments)
            avg_cost_per_new_request = total_cost / new_assignments if new_assignments else 0.0
            avg_cost_per_assignment = total_cost / total_assignments if total_assignments else 0.0

            # Add assignments to shared state
            self.shared_state.add_assignments(assignments)

            # Remove only newly scheduled pending requests from queue.
            # Re-planned future assignments (if enabled) are not in pending.
            self.shared_state.pop_pending_requests(len(pending))

            with self._lock:
                self._batches_processed += 1
                self._total_scheduled += len(pending)
                self._solver_total_time_ms += solver_elapsed_ms
                self._solver_total_runs += 1
                self._solver_total_requests += len(pending)
                self._last_solver_elapsed_ms = solver_elapsed_ms

            if config.VERBOSE:
                avg_error = sum(a.error for a in assignments) / len(assignments)
                replanned = max(0, len(assignments) - len(pending))
                print(
                    f"[Scheduler] ✓ Scheduled {len(pending)} new requests"
                    f"{' + ' + str(replanned) + ' re-planned' if replanned else ''}"
                    f" (cost={total_cost:.2f}, cost/new={avg_cost_per_new_request:.2f}, "
                    f"error={avg_error:.2f}%, "
                    f"solver={solver_elapsed_ms:.2f}ms, {avg_ms_per_new_request:.2f}ms/req)"
                )

            # Export to CSV
            self.shared_state.export_to_csv(config.OUTPUT_FILE)

            # Log only successful scheduling runs (with actual assignments).
            all_assignments_after = list(self.shared_state.get_current_assignments().values())
            real_error_window_after = self.shared_state.get_window_error_stats(
                center_slot=current_slot,
                window_past=config.ERROR_WINDOW_PAST,
                window_future=config.ERROR_WINDOW_FUTURE,
            )
            modeled_error_window_avg_after = float(
                solve_context.get(
                    "modeled_window_avg_after",
                    real_error_window_after.get("average_error", 0.0),
                )
            )
            window_start = int(
                solve_context.get(
                    "window_start_slot",
                    max(0, current_slot - int(config.ERROR_WINDOW_PAST)),
                )
            )
            window_end = int(
                solve_context.get(
                    "window_end_slot",
                    min(int(config.TOTAL_SLOTS) - 1, current_slot + int(config.ERROR_WINDOW_FUTURE)),
                )
            )
            slot_metrics = self._build_slot_metrics(
                assignments=assignments,
                current_slot=current_slot,
            )
            assignment_rows = self._build_assignment_rows(
                assignments=all_assignments_after,
                new_assignment_ids={a.request_id for a in assignments},
                pending_ids=solve_context.get("pending_ids", set()),
                current_slot=current_slot,
                solver_start_ts=solver_start_wall,
                solver_end_ts=solver_end_wall,
            )
            run_row = {
                "run_sequence": self._solver_total_runs,
                "current_slot": current_slot,
                "pending_batch_size": len(pending),
                "total_assignments": total_assignments,
                "new_assignments": new_assignments,
                "replanned_assignments": replanned_assignments,
                "solver_status": solve_context.get("status", "unknown"),
                "solver_mode": solve_context.get("mode", "dp"),
                "lock_future_assignments": config.DP_LOCK_FUTURE_ASSIGNMENTS,
                "solver_start_ts": solver_start_wall,
                "solver_end_ts": solver_end_wall,
                "solver_elapsed_ms": solver_elapsed_ms,
                "avg_ms_per_new_request": avg_ms_per_new_request,
                "avg_ms_per_assignment": avg_ms_per_assignment,
                "total_carbon_cost": total_cost,
                "carbon_cost_per_new_request": avg_cost_per_new_request,
                "carbon_cost_per_assignment": avg_cost_per_assignment,
                "error_window_avg_after": modeled_error_window_avg_after,
                "error_window_avg_after_real": real_error_window_after.get("average_error", 0.0),
                "error_window_start_slot": window_start,
                "error_window_end_slot": window_end,
                "error_window_threshold": config.MAX_ERROR_THRESHOLD,
                "error_window_violated_after": (
                    modeled_error_window_avg_after > float(config.MAX_ERROR_THRESHOLD)
                ),
                "error_window_violated_after_real": (
                    real_error_window_after.get("average_error", 0.0) > float(config.MAX_ERROR_THRESHOLD)
                ),
                "batches_processed_after": self._batches_processed,
                "total_scheduled_after": self._total_scheduled,
            }
            self.metrics_logger.log_solver_run(
                run_data=run_row,
                assignment_rows=assignment_rows,
                slot_metric_rows=slot_metrics,
            )
            return True

        return False

    def _solve_dp(self, requests: List[Request], current_slot: int) -> Tuple[List[Assignment], Dict]:
        """
        Solve batch scheduling using DP with optional Beam Search pruning.
        
        Uses RollingWindowDPScheduler to find optimal batch assignment.
        
        Args:
            requests: Batch of requests to schedule
            current_slot: Current slot index
            
        Returns:
            Tuple (assignments, context)
        """
        pending_ids: Set[int] = {req.id for req in requests}
        solve_context: Dict = {"pending_ids": pending_ids, "status": "ok", "mode": "dp"}
        pending_metadata = {
            req.id: {"arrival_slot": req.arrival_slot, "deadline_slot": req.deadline_slot}
            for req in requests
        }
        assignment_cap_slot = min(
            int(config.TOTAL_SLOTS) - 1,
            current_slot + int(getattr(config, "ASSIGNMENT_MAX_FUTURE_SLOTS", config.ERROR_WINDOW_FUTURE)),
        )

        def _cap_deadline(deadline_slot: Optional[int]) -> int:
            raw = int(deadline_slot) if deadline_slot is not None else assignment_cap_slot
            return max(current_slot, min(raw, assignment_cap_slot, int(config.TOTAL_SLOTS) - 1))

        future_assignments = self.shared_state.get_future_assignments(current_slot)
        future_ids = {a.request_id for a in future_assignments}

        dp_requests = [{"id": req.id, "deadline_slot": _cap_deadline(req.deadline_slot)} for req in requests]
        assignment_metadata = dict(pending_metadata)

        fixed_future_assignments: List[Assignment] = []
        movable_future_ids: Set[int] = set()

        if config.DP_LOCK_FUTURE_ASSIGNMENTS:
            fixed_future_assignments = future_assignments
        else:
            movable_future_ids = future_ids
            for assignment in future_assignments:
                inferred_deadline = assignment.deadline_slot
                if inferred_deadline is None:
                    inferred_deadline = max(current_slot, assignment.scheduled_slot)
                dp_requests.append(
                    {
                        "id": assignment.request_id,
                        "deadline_slot": _cap_deadline(inferred_deadline),
                    }
                )
                assignment_metadata[assignment.request_id] = {
                    "arrival_slot": assignment.arrival_slot,
                    "deadline_slot": _cap_deadline(inferred_deadline),
                }

        # Baseline load from fixed assignments that remain pinned
        baseline_slot_counts: Dict[int, int] = {}
        baseline_slot_durations: Dict[int, int] = {}
        for assignment in fixed_future_assignments:
            slot = assignment.scheduled_slot
            baseline_slot_counts[slot] = baseline_slot_counts.get(slot, 0) + 1
            duration = assignment.strategy_duration or self.strategy_duration_by_name.get(assignment.strategy_name, 0)
            baseline_slot_durations[slot] = baseline_slot_durations.get(slot, 0) + duration
        solve_context["baseline_slot_counts"] = baseline_slot_counts

        # Weighted error baseline: total error / total requests in the window
        error_baseline = self.shared_state.get_window_error_stats(
            center_slot=current_slot,
            window_past=config.ERROR_WINDOW_PAST,
            window_future=config.ERROR_WINDOW_FUTURE,
            exclude_request_ids=movable_future_ids,
        )
        error_baseline, prehistory_ctx = self._augment_error_baseline_with_virtual_past(
            current_slot=current_slot,
            error_baseline=error_baseline,
        )
        solve_context.update(prehistory_ctx)
        error_baseline, dynamic_mock_pool, recovery_ctx = self._apply_infeasibility_recovery_policy(
            current_slot=current_slot,
            error_baseline=error_baseline,
        )
        solve_context.update(recovery_ctx)
        if solve_context.get("virtual_past_slots_used", 0) > 0 and config.VERBOSE:
            print(
                "[Scheduler] ℹ Virtual pre-history applied: "
                f"slots={solve_context['virtual_past_slots_used']}, "
                f"virtual_requests={solve_context['virtual_past_requests']}, "
                f"avg_error={solve_context['virtual_past_avg_error']:.2f}%"
            )

        try:
            dp_assignments = self.dp_solver.solve_batch(
                requests=dp_requests,
                current_slot=current_slot,
                capacity_tiers=config.CAPACITY_TIERS,
                baseline_slot_counts=baseline_slot_counts,
                baseline_slot_durations=baseline_slot_durations,
                error_window_baseline=error_baseline,
                max_error_threshold=config.MAX_ERROR_THRESHOLD,
                error_window_past=config.ERROR_WINDOW_PAST,
                error_window_future=config.ERROR_WINDOW_FUTURE,
                assignment_max_slot=assignment_cap_slot,
                dynamic_mock_pool=dynamic_mock_pool,
            )
        except Exception as e:
            if config.VERBOSE:
                print(f"[Scheduler] ✗ DP solver error: {e}, falling back to greedy")
            solve_context["mode"] = "greedy_fallback"
            dp_assignments = self.dp_solver._greedy_fallback(
                requests=dp_requests,
                deadlines=[max(current_slot, min(r["deadline_slot"], config.TOTAL_SLOTS - 1)) for r in dp_requests],
                current_slot=current_slot,
                capacity_tiers=config.CAPACITY_TIERS,
            )

        scheduled_pending_ids = {a.request_id for a in dp_assignments if a.request_id in pending_ids}
        if len(scheduled_pending_ids) != len(pending_ids):
            if config.VERBOSE:
                print("[Scheduler] ⚠ Infeasible with strict error window: retry with relaxed window.")

            relaxed_assignments, relaxed_mode = self._solve_relaxed_retry(
                dp_requests=dp_requests,
                current_slot=current_slot,
                baseline_slot_counts=baseline_slot_counts,
                baseline_slot_durations=baseline_slot_durations,
                error_baseline=error_baseline,
                assignment_cap_slot=assignment_cap_slot,
                dynamic_mock_pool=dynamic_mock_pool,
                recovery_mode=solve_context.get("infeasibility_recovery_mode", "min_error_recovery"),
            )
            relaxed_pending_ids = {a.request_id for a in relaxed_assignments if a.request_id in pending_ids}
            debug_event_id = self._log_strict_infeasibility_debug(
                current_slot=current_slot,
                pending_requests=requests,
                pending_ids=pending_ids,
                future_assignments=future_assignments,
                baseline_slot_counts=baseline_slot_counts,
                error_baseline=error_baseline,
                strict_scheduled_pending_count=len(scheduled_pending_ids),
                relaxed_scheduled_pending_count=len(relaxed_pending_ids),
            )
            if debug_event_id and config.VERBOSE:
                print(f"[Scheduler] ℹ Strict infeasibility debug logged: event_id={debug_event_id}")
            if len(relaxed_pending_ids) == len(pending_ids):
                dp_assignments = relaxed_assignments
                solve_context["status"] = "ok_relaxed"
                solve_context["mode"] = relaxed_mode
            elif relaxed_mode == "dp_relaxed_disabled":
                solve_context["status"] = "infeasible_strict"
                solve_context["mode"] = "dp_strict_only"
                dp_assignments = []
            else:
                if config.VERBOSE:
                    print("[Scheduler] ⚠ Still infeasible: forcing greedy scheduling for pending requests.")

                base_counts_arr = [0] * config.TOTAL_SLOTS
                base_durations_arr = [0] * config.TOTAL_SLOTS
                for slot, count in baseline_slot_counts.items():
                    if 0 <= slot < config.TOTAL_SLOTS:
                        base_counts_arr[slot] = int(count)
                for slot, dur in baseline_slot_durations.items():
                    if 0 <= slot < config.TOTAL_SLOTS:
                        base_durations_arr[slot] = int(dur)

                pending_only_requests = [{"id": req.id, "deadline_slot": req.deadline_slot} for req in requests]
                pending_only_deadlines = [
                    _cap_deadline(req.deadline_slot)
                    for req in requests
                ]
                dp_assignments = self.dp_solver._greedy_fallback(
                    requests=pending_only_requests,
                    deadlines=pending_only_deadlines,
                    current_slot=current_slot,
                    capacity_tiers=config.CAPACITY_TIERS,
                    base_counts=base_counts_arr,
                    base_durations=base_durations_arr,
                )
                solve_context["status"] = "ok_greedy_after_infeasible"
                solve_context["mode"] = "greedy_after_infeasible"

        # Safety check: if pending requests are still not all covered, keep retry behavior.
        scheduled_pending_ids = {a.request_id for a in dp_assignments if a.request_id in pending_ids}
        if len(scheduled_pending_ids) != len(pending_ids):
            if config.VERBOSE:
                print("[Scheduler] ⚠ Infeasible batch under current constraints; retrying later.")
            solve_context["status"] = "infeasible"
            return [], solve_context
        
        # Convert RequestAssignment objects to Assignment objects
        assignments = []
        for dp_assignment in dp_assignments:
            metadata = assignment_metadata.get(dp_assignment.request_id, {})
            strategy_duration = self.strategy_duration_by_name.get(dp_assignment.strategy_name, 0)
            assignment = Assignment(
                request_id=dp_assignment.request_id,
                scheduled_slot=dp_assignment.slot,
                strategy_name=dp_assignment.strategy_name,
                carbon_cost=dp_assignment.carbon_cost,
                error=dp_assignment.error,
                strategy_duration=strategy_duration,
                arrival_slot=metadata.get("arrival_slot"),
                deadline_slot=metadata.get("deadline_slot"),
            )
            assignments.append(assignment)

        window_start = max(0, current_slot - int(config.ERROR_WINDOW_PAST))
        window_end = min(int(config.TOTAL_SLOTS) - 1, current_slot + int(config.ERROR_WINDOW_FUTURE))
        solve_context["window_start_slot"] = window_start
        solve_context["window_end_slot"] = window_end
        modeled_error_sum = float(error_baseline.get("error_sum", 0.0))
        modeled_request_count = int(error_baseline.get("request_count", 0))
        mock_remaining = int(dynamic_mock_pool.get("initial_count", 0))
        mock_error = float(dynamic_mock_pool.get("error_per_request", 0.0))
        for assignment in assignments:
            if window_start <= int(assignment.scheduled_slot) <= window_end:
                modeled_error_sum += float(assignment.error)
                modeled_request_count += 1
                if mock_remaining > 0 and mock_error > 0.0:
                    modeled_error_sum -= mock_error
                    modeled_request_count = max(0, modeled_request_count - 1)
                    mock_remaining -= 1
        solve_context["modeled_window_avg_after"] = (
            modeled_error_sum / modeled_request_count if modeled_request_count > 0 else 0.0
        )
        solve_context["modeled_window_request_count_after"] = modeled_request_count

        return assignments, solve_context

    def _augment_error_baseline_with_virtual_past(
        self,
        current_slot: int,
        error_baseline: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict]:
        """
        Add virtual pre-history to the error baseline for startup iterations.

        For current_slot < W (W=ERROR_WINDOW_PAST), we assume W missing past slots
        before slot 0. Each virtual slot contributes a request count tied to the
        predicted arrival rate and a mean error equal to half threshold.
        """
        base_error_sum = float(error_baseline.get("error_sum", 0.0))
        base_request_count = int(error_baseline.get("request_count", 0))
        context = {
            "virtual_past_slots_used": 0,
            "virtual_past_requests": 0,
            "virtual_past_avg_error": config.MAX_ERROR_THRESHOLD * config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD,
        }

        if not getattr(config, "PREHISTORY_USE_VIRTUAL_PAST", False):
            return error_baseline, context

        W = int(config.ERROR_WINDOW_PAST)
        missing_past_slots = max(0, W - int(current_slot))
        if missing_past_slots <= 0:
            return error_baseline, context

        expected_rate = float(config.PREDICTED_REQUESTS_PER_SLOT)
        sigma = max(1.0, expected_rate * float(config.REQUEST_RATE_STD_FACTOR))
        virtual_avg_error = float(config.MAX_ERROR_THRESHOLD) * float(config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD)

        virtual_requests = 0
        for slot_offset in range(-missing_past_slots, 0):
            if getattr(config, "PREHISTORY_STOCHASTIC_COUNTS", True):
                rng = random.Random(int(config.PREHISTORY_RANDOM_SEED) + slot_offset)
                virtual_count = max(1, int(rng.gauss(expected_rate, sigma)))
            else:
                virtual_count = max(1, int(round(expected_rate)))
            virtual_requests += virtual_count

        augmented = {
            "error_sum": base_error_sum + (virtual_requests * virtual_avg_error),
            "request_count": base_request_count + virtual_requests,
            "average_error": (
                (base_error_sum + (virtual_requests * virtual_avg_error)) / (base_request_count + virtual_requests)
                if (base_request_count + virtual_requests) > 0
                else 0.0
            ),
        }
        context.update(
            {
                "virtual_past_slots_used": missing_past_slots,
                "virtual_past_requests": virtual_requests,
                "virtual_past_avg_error": virtual_avg_error,
            }
        )
        return augmented, context

    def _apply_infeasibility_recovery_policy(
        self,
        current_slot: int,
        error_baseline: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict]:
        """
        Apply one of three recovery policies to the strict error baseline.

        Returns:
            (augmented_baseline, dynamic_mock_pool, context)
        """
        mode = str(getattr(config, "INFEASIBILITY_RECOVERY_MODE", "min_error_recovery")).strip().lower()
        context = {
            "infeasibility_recovery_mode": mode,
            "mock_recovery_count": 0,
            "mock_recovery_error": 0.0,
        }
        dynamic_mock_pool = {"initial_count": 0, "error_per_request": 0.0}

        if mode == "min_error_recovery":
            return error_baseline, dynamic_mock_pool, context

        augmented = dict(error_baseline)
        mock_count = 0
        mock_error = 0.0

        if mode == "carryover_last_slot":
            window_start = max(0, current_slot - int(config.ERROR_WINDOW_PAST))
            dropped_slot = window_start - 1
            if dropped_slot >= 0:
                dropped_assignments = self.shared_state.get_requests_in_slot(dropped_slot)
                mock_count = len(dropped_assignments)
                if mock_count > 0:
                    mock_error = sum(a.error for a in dropped_assignments) / mock_count

        elif mode == "forecast_mock_current_slot":
            expected_rate = float(config.PREDICTED_REQUESTS_PER_SLOT)
            sigma = max(1.0, expected_rate * float(config.REQUEST_RATE_STD_FACTOR))
            rng = random.Random(int(config.PREHISTORY_RANDOM_SEED) + int(current_slot))
            mock_count = max(0, int(rng.gauss(expected_rate, sigma)))
            mock_error = float(config.MAX_ERROR_THRESHOLD) * float(config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD)

        if mock_count > 0 and mock_error > 0.0:
            augmented_error_sum = float(augmented.get("error_sum", 0.0)) + mock_count * mock_error
            augmented_request_count = int(augmented.get("request_count", 0)) + mock_count
            augmented = {
                "error_sum": augmented_error_sum,
                "request_count": augmented_request_count,
                "average_error": augmented_error_sum / augmented_request_count if augmented_request_count > 0 else 0.0,
            }
            dynamic_mock_pool = {"initial_count": mock_count, "error_per_request": mock_error}
            context.update(
                {
                    "mock_recovery_count": mock_count,
                    "mock_recovery_error": mock_error,
                }
            )

        return augmented, dynamic_mock_pool, context
    
    def _get_carbon_forecast(self) -> List[float]:
        """
        Generate carbon intensity forecast for all time slots.
        Uses sinusoidal pattern (realistic day-night cycle).
        
        Returns:
            List of carbon intensity values [0..TOTAL_SLOTS-1]
        """
        forecast = []
        num_slots = config.TOTAL_SLOTS
        base_carbon = 500
        amplitude = 400
        
        K = 6

        for slot in range(num_slots):
            phase = 2 * math.pi * (slot % K) / K  # ciclo su K
            value = base_carbon + amplitude * (1 + 0.8 * math.cos(phase))
            forecast.append(max(100, value))
        
        return forecast

    def get_statistics(self) -> Dict:
        """Get scheduler statistics"""
        with self._lock:
            avg_solver_ms_per_batch = self._solver_total_time_ms / self._solver_total_runs if self._solver_total_runs else 0.0
            avg_solver_ms_per_request = self._solver_total_time_ms / self._solver_total_requests if self._solver_total_requests else 0.0
            return {
                "batches_processed": self._batches_processed,
                "total_scheduled": self._total_scheduled,
                "solver_runs": self._solver_total_runs,
                "last_solver_elapsed_ms": self._last_solver_elapsed_ms,
                "avg_solver_ms_per_batch": avg_solver_ms_per_batch,
                "avg_solver_ms_per_request": avg_solver_ms_per_request,
            }

    def _get_capacity_tier_info(self, request_count: int):
        for tier in config.CAPACITY_TIERS:
            if request_count <= tier["max_requests"]:
                return float(tier["multiplier"]), tier["max_requests"]
        return float(config.CAPACITY_TIERS[-1]["multiplier"]), config.CAPACITY_TIERS[-1]["max_requests"]

    def _build_slot_metrics(
        self,
        assignments: List[Assignment],
        current_slot: int,
    ) -> List[Dict]:
        grouped = defaultdict(list)
        for assignment in assignments:
            grouped[assignment.scheduled_slot].append(assignment)

        rows: List[Dict] = []
        for slot in range(config.TOTAL_SLOTS):
            slot_assignments = grouped.get(slot, [])
            run_slot_count = len(slot_assignments)
            run_avg_error = sum(a.error for a in slot_assignments) / run_slot_count if run_slot_count else 0.0

            total_slot_assignments = self.shared_state.get_requests_in_slot(slot)
            total_after = len(total_slot_assignments)
            total_avg_error = (
                sum(a.error for a in total_slot_assignments) / total_after if total_after else 0.0
            )
            multiplier, tier_max = self._get_capacity_tier_info(total_after)
            request_ids = "|".join(str(a.request_id) for a in slot_assignments)
            strategy_counts = defaultdict(int)
            for a in slot_assignments:
                strategy_counts[a.strategy_name] += 1
            strategy_breakdown = "|".join(
                f"{strategy}:{count}" for strategy, count in sorted(strategy_counts.items())
            )

            rows.append(
                {
                    "current_slot": current_slot,
                    "scheduled_slot": slot,
                    "run_slot_count": run_slot_count,
                    "total_slot_count_after": total_after,
                    "avg_error_in_slot": total_avg_error,
                    "run_avg_error_in_slot": run_avg_error,
                    "slot_has_assignments_after": total_after > 0,
                    "carbon_intensity": self.carbon_forecast[slot] if 0 <= slot < len(self.carbon_forecast) else 0.0,
                    "capacity_multiplier_after": multiplier,
                    "capacity_level_max_requests": tier_max,
                    "request_ids": request_ids,
                    "strategy_breakdown": strategy_breakdown,
                }
            )
        return rows

    def _solve_relaxed_retry(
        self,
        dp_requests: List[Dict],
        current_slot: int,
        baseline_slot_counts: Dict[int, int],
        baseline_slot_durations: Dict[int, int],
        error_baseline: Dict[str, float],
        assignment_cap_slot: int,
        dynamic_mock_pool: Dict[str, float],
        recovery_mode: str,
    ) -> Tuple[List, str]:
        if (
            not getattr(config, "DP_ALLOW_RELAXED_ERROR_RETRY", True)
            and recovery_mode != "min_error_recovery"
        ):
            return [], "dp_relaxed_disabled"

        preferred_mode = "dp_relaxed_error"
        original_strategies = self.dp_solver.strategies
        relaxed_strategies = original_strategies

        prefer_min_error = (
            recovery_mode == "min_error_recovery"
            or getattr(config, "DP_RELAXED_RETRY_PREFER_MIN_ERROR", False)
        )
        if prefer_min_error and original_strategies:
            min_error = min(float(s["error"]) for s in original_strategies)
            relaxed_strategies = [
                s for s in original_strategies if abs(float(s["error"]) - min_error) < 1e-9
            ]
            if relaxed_strategies:
                preferred_mode = "dp_relaxed_min_error"

        try:
            self.dp_solver.strategies = relaxed_strategies
            relaxed_assignments = self.dp_solver.solve_batch(
                requests=dp_requests,
                current_slot=current_slot,
                capacity_tiers=config.CAPACITY_TIERS,
                baseline_slot_counts=baseline_slot_counts,
                baseline_slot_durations=baseline_slot_durations,
                error_window_baseline=error_baseline,
                max_error_threshold=None,
                error_window_past=config.ERROR_WINDOW_PAST,
                error_window_future=config.ERROR_WINDOW_FUTURE,
                assignment_max_slot=assignment_cap_slot,
                dynamic_mock_pool=dynamic_mock_pool,
            )
        except Exception as e:
            if config.VERBOSE:
                print(f"[Scheduler] ✗ Relaxed DP retry failed: {e}")
            relaxed_assignments = []
            preferred_mode = "dp_relaxed_failed"
        finally:
            self.dp_solver.strategies = original_strategies

        return relaxed_assignments, preferred_mode

    def _build_assignment_rows(
        self,
        assignments: List[Assignment],
        new_assignment_ids: Set[int],
        pending_ids: Set[int],
        current_slot: int,
        solver_start_ts: float,
        solver_end_ts: float,
    ) -> List[Dict]:
        rows: List[Dict] = []
        for assignment in sorted(assignments, key=lambda a: (a.scheduled_slot, a.request_id)):
            rows.append(
                {
                    "current_slot": current_slot,
                    "solver_start_ts": solver_start_ts,
                    "solver_end_ts": solver_end_ts,
                    "request_id": assignment.request_id,
                    "is_pending_request": assignment.request_id in pending_ids,
                    "is_new_assignment_in_run": assignment.request_id in new_assignment_ids,
                    "scheduled_slot": assignment.scheduled_slot,
                    "strategy_name": assignment.strategy_name,
                    "strategy_duration": assignment.strategy_duration,
                    "error": assignment.error,
                    "carbon_cost": assignment.carbon_cost,
                    "arrival_slot": assignment.arrival_slot,
                    "deadline_slot": assignment.deadline_slot,
                }
            )
        return rows

    def _log_strict_infeasibility_debug(
        self,
        current_slot: int,
        pending_requests: List[Request],
        pending_ids: Set[int],
        future_assignments: List[Assignment],
        baseline_slot_counts: Dict[int, int],
        error_baseline: Dict[str, float],
        strict_scheduled_pending_count: int,
        relaxed_scheduled_pending_count: int,
    ) -> str:
        if not getattr(config, "ENABLE_INFEASIBILITY_DEBUG_LOGGING", False):
            return ""

        min_strategy_error = min((s.error for s in self.strategies), default=0.0)
        max_strategy_error = max((s.error for s in self.strategies), default=0.0)

        baseline_error_sum = float(error_baseline.get("error_sum", 0.0))
        baseline_request_count = int(error_baseline.get("request_count", 0))
        baseline_average_error = (
            baseline_error_sum / baseline_request_count if baseline_request_count > 0 else 0.0
        )

        pending_count = len(pending_requests)
        denominator = baseline_request_count + pending_count
        min_possible_avg = (
            (baseline_error_sum + pending_count * min_strategy_error) / denominator
            if denominator > 0
            else 0.0
        )
        max_possible_avg = (
            (baseline_error_sum + pending_count * max_strategy_error) / denominator
            if denominator > 0
            else 0.0
        )

        active_assignments = self.shared_state.get_current_assignments()
        active_slot_counts: Dict[int, int] = defaultdict(int)
        for assignment in active_assignments.values():
            active_slot_counts[int(assignment.scheduled_slot)] += 1

        future_slot_counts: Dict[int, int] = defaultdict(int)
        for assignment in future_assignments:
            future_slot_counts[int(assignment.scheduled_slot)] += 1

        pending_request_details = "|".join(
            f"{req.id}:{max(current_slot, req.deadline_slot)}"
            for req in sorted(pending_requests, key=lambda r: (r.deadline_slot, r.id))
        )
        future_assignment_details = "|".join(
            f"{a.request_id}:{a.scheduled_slot}:{a.deadline_slot if a.deadline_slot is not None else ''}:{a.strategy_name}"
            for a in sorted(future_assignments, key=lambda x: (x.scheduled_slot, x.request_id))
        )
        future_slot_counts_serialized = "|".join(
            f"{slot}:{count}" for slot, count in sorted(future_slot_counts.items())
        )
        active_slot_counts_serialized = "|".join(
            f"{slot}:{count}" for slot, count in sorted(active_slot_counts.items())
        )

        strict_threshold = float(config.MAX_ERROR_THRESHOLD)
        event = {
            "current_slot": current_slot,
            "pending_batch_size": pending_count,
            "pending_request_details": pending_request_details,
            "strict_threshold": strict_threshold,
            "baseline_error_sum": baseline_error_sum,
            "baseline_request_count": baseline_request_count,
            "baseline_average_error": baseline_average_error,
            "min_strategy_error": min_strategy_error,
            "max_strategy_error": max_strategy_error,
            "min_possible_avg_error_pending_only": min_possible_avg,
            "max_possible_avg_error_pending_only": max_possible_avg,
            "strict_infeasible_by_error_bound": min_possible_avg > strict_threshold,
            "strict_scheduled_pending_count": strict_scheduled_pending_count,
            "relaxed_scheduled_pending_count": relaxed_scheduled_pending_count,
            "relaxed_success": relaxed_scheduled_pending_count == len(pending_ids),
            "lock_future_assignments": config.DP_LOCK_FUTURE_ASSIGNMENTS,
            "future_assignment_count": len(future_assignments),
            "future_slot_counts": future_slot_counts_serialized,
            "future_assignment_details": future_assignment_details,
            "all_active_slot_counts": active_slot_counts_serialized,
        }
        return self.metrics_logger.log_infeasible_debug(event)
