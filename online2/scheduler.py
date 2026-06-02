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
class Flavour:
    """Execution flavour definition (name, error %, duration)"""
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
        self.flavours = [Flavour(**s) for s in config.FLAVOURS]
        self.flavour_duration_by_name = {s["name"]: int(s["duration"]) for s in config.FLAVOURS}

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._active_batch_workers: Set[threading.Thread] = set()

        # Statistics
        self._batches_processed = 0
        self._total_scheduled = 0
        self._solver_total_time_ms = 0.0
        self._solver_total_runs = 0
        self._solver_total_requests = 0
        self._last_solver_elapsed_ms = 0.0
        self._last_infeasible_slot: Optional[int] = None
        self._last_infeasible_pending: Optional[int] = None
        self._mock_influence_base = self._clamp_mock_influence(
            config.INFEASIBILITY_MOCK_INFLUENCE
        )
        self._mock_influence_effective = self._mock_influence_base
        self._mock_influence_above_threshold_streak = 0
        self._mock_influence_last_eval_slot: Optional[int] = None
        self._persistent_mock_slot: Optional[int] = None
        self._persistent_mock_mode: Optional[str] = None
        self._persistent_mock_remaining = 0
        self._persistent_mock_error = 0.0

        # Initialize DP solver
        self.carbon_forecast = self._get_carbon_forecast()
        self.dp_solver = RollingWindowDPScheduler(
            flavours=list(config.FLAVOURS),
            carbon_forecast=self.carbon_forecast,
            window_size=config.TOTAL_SLOTS,
            pruning=config.DP_PRUNING_METHOD,
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
                if config.ENABLE_INFEASIBILITY_DEBUG_LOGGING
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
            print(
                "[Scheduler] Started "
                f"(batch_size={config.BATCH_SIZE}, "
                f"max_parallel={self._get_max_batch_parallelism()})"
            )

    def stop(self) -> None:
        """Stop scheduler thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self._join_active_batch_workers(timeout=5.0)

        if config.VERBOSE:
            print(f"[Scheduler] Stopped (processed {self._batches_processed} batches)")

    def _run(self) -> None:
        """Main scheduler loop (runs in thread)"""
        slot_duration = config.SLOT_DURATION_SECONDS
        slot_start_time = time.time()

        while self._running:
            self._cleanup_completed_batch_workers()
            now = time.time()
            elapsed = now - slot_start_time
            slot = int(elapsed / slot_duration)

            # Update current slot in shared state
            self.shared_state.set_current_slot(slot)

            # Check if we have enough pending requests
            pending_count = self.shared_state.get_pending_count()
            active_workers = self._get_active_batch_worker_count()
            max_parallel = self._get_max_batch_parallelism()

            if pending_count >= config.BATCH_SIZE and active_workers < max_parallel:
                if config.VERBOSE:
                    print(
                        f"\n[Scheduler] Slot {slot}: {pending_count} pending, "
                        f"active_workers={active_workers}/{max_parallel}"
                    )
                self._dispatch_batch_workers_for_slot(slot)

            # Small sleep
            time.sleep(0.1)

    def _dispatch_batch_workers_for_slot(self, slot: int) -> None:
        """
        Dispatch as many batch workers as possible for the current slot,
        constrained by pending queue size and max parallelism.
        """
        while self._running:
            self._cleanup_completed_batch_workers()
            pending_count = self.shared_state.get_pending_count()
            active_workers = self._get_active_batch_worker_count()
            max_parallel = self._get_max_batch_parallelism()

            if pending_count < config.BATCH_SIZE or active_workers >= max_parallel:
                return

            # Avoid retry storm: if same slot and same pending count were just
            # infeasible, wait for slot/pending change before retrying.
            if (
                self._last_infeasible_slot == slot
                and self._last_infeasible_pending == pending_count
            ):
                return

            pending = self.shared_state.claim_pending_requests(config.BATCH_SIZE)
            if len(pending) < config.BATCH_SIZE:
                self.shared_state.requeue_pending_requests_front(pending)
                return

            self._start_batch_worker(slot, pending)

    def _start_batch_worker(self, current_slot: int, pending: List[Request]) -> None:
        """
        Start one short-lived worker thread for a claimed batch.
        """
        worker = threading.Thread(
            target=self._batch_worker_entry,
            args=(current_slot, pending),
            daemon=False,
        )
        with self._lock:
            self._active_batch_workers.add(worker)
        worker.start()

    def _batch_worker_entry(self, current_slot: int, pending: List[Request]) -> None:
        """
        Worker body for one claimed batch.
        """
        try:
            if config.VERBOSE:
                print(
                    f"[Scheduler] Worker start: slot={current_slot}, "
                    f"batch_size={len(pending)}"
                )

            scheduled = self._process_batch(current_slot, pending_override=pending)

            with self._lock:
                if scheduled:
                    self._last_infeasible_slot = None
                    self._last_infeasible_pending = None
                else:
                    self._last_infeasible_slot = current_slot
                    self._last_infeasible_pending = len(pending)
        finally:
            self._cleanup_completed_batch_workers()

    def _cleanup_completed_batch_workers(self) -> None:
        """
        Drop finished worker threads from the active set.
        """
        with self._lock:
            completed = [worker for worker in self._active_batch_workers if not worker.is_alive()]
            for worker in completed:
                self._active_batch_workers.discard(worker)

    def _join_active_batch_workers(self, timeout: float = 5.0) -> None:
        """
        Join all active batch workers, then clean up the active set.
        """
        with self._lock:
            workers = list(self._active_batch_workers)

        for worker in workers:
            worker.join(timeout=timeout)

        self._cleanup_completed_batch_workers()

    def _get_active_batch_worker_count(self) -> int:
        with self._lock:
            return len(self._active_batch_workers)

    def _get_max_batch_parallelism(self) -> int:
        configured = int(config.MAX_BATCH_SOLVER_PARALLELISM)
        return max(1, configured)

    def _process_batch(
        self,
        current_slot: int,
        pending_override: Optional[List[Request]] = None,
    ) -> bool:
        """
        Process a batch of pending requests.
        
        Args:
            current_slot: Current time slot
        """
        if pending_override is None:
            pending = self.shared_state.claim_pending_requests(config.BATCH_SIZE)
        else:
            pending = list(pending_override)

        if not pending:
            return False

        scheduled = self._process_claimed_batch(current_slot=current_slot, pending=pending)
        if not scheduled:
            self.shared_state.requeue_pending_requests_front(pending)
        return scheduled

    def _process_claimed_batch(self, current_slot: int, pending: List[Request]) -> bool:
        """
        Process a pre-claimed batch of pending requests.
        """
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
                    self._error_window_bounds(current_slot)[0],
                )
            )
            window_end = int(
                solve_context.get(
                    "window_end_slot",
                    self._error_window_bounds(current_slot)[1],
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
        Core scheduling pipeline for one batch.

        Pipeline (in order):
        1. Build per-request deadline caps (assignment_cap = end of error window).
        2. Optionally include movable future assignments for joint re-planning
           (DP_LOCK_FUTURE_ASSIGNMENTS=False) or pin them as baseline load (True).
        3. Construct the weighted error baseline:
           a. Real window error from shared state.
           b. Decayed extension of the window's past boundary (smoothing).
           c. Virtual prehistory for startup slots (< ERROR_WINDOW_PAST).
           d. Infeasibility recovery augmentation (mock requests injection).
        4. Apply global error constraint — hard mode filters the flavour list.
        5. Run DP solver; on infeasibility retry with relaxed window, then greedy.
        6. Convert RequestAssignment → Assignment, compute modelled window avg.

        Returns:
            (assignments, solve_context) — empty list + status='infeasible' if
            the batch cannot be scheduled at all.
        """
        effective_pruning = self._get_effective_pruning_mode(len(requests))
        solver = self._create_batch_solver(pruning_mode=effective_pruning)

        pending_ids: Set[int] = {req.id for req in requests}
        solve_context: Dict = {
            "pending_ids": pending_ids,
            "status": "ok",
            "mode": "dp",
            "pruning_mode": effective_pruning,
            "pruning_min_batch_size": int(config.DP_PRUNING_MIN_BATCH_SIZE),
        }
        pending_metadata = {
            req.id: {"arrival_slot": req.arrival_slot, "deadline_slot": req.deadline_slot}
            for req in requests
        }
        _, window_end = self._error_window_bounds(current_slot)
        # Enforce assignment cap at the end of the error window.
        assignment_cap_slot = window_end

        def _cap_deadline(deadline_slot: Optional[int]) -> int:
            raw = int(deadline_slot) if deadline_slot is not None else assignment_cap_slot
            return max(current_slot, min(raw, assignment_cap_slot, int(config.TOTAL_SLOTS) - 1))

        # ── Step 2: time-shifting (future assignment handling) ────────────────
        # If DP_LOCK_FUTURE_ASSIGNMENTS=True, existing future assignments are kept
        # fixed and their load is injected as baseline counts/durations.
        # If False, they are released back into the DP pool for joint re-planning
        # (movable_future_ids are excluded from the error baseline query).
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
            duration = assignment.flavour_duration or self.flavour_duration_by_name.get(assignment.flavour_name, 0)
            baseline_slot_durations[slot] = baseline_slot_durations.get(slot, 0) + duration
        solve_context["baseline_slot_counts"] = baseline_slot_counts

        # ── Step 3: error baseline construction ──────────────────────────────
        # The baseline is a weighted {error_sum, request_count, average_error}
        # dict that represents requests already scheduled in the error window
        # [current_slot-W_past .. current_slot+W_future].  It is augmented in
        # three passes before being handed to the DP solver.

        # 3a. Real window error (excludes movable future ids for re-planning).
        error_baseline = self.shared_state.get_window_error_stats(
            center_slot=current_slot,
            window_past=config.ERROR_WINDOW_PAST,
            window_future=config.ERROR_WINDOW_FUTURE,
            exclude_request_ids=movable_future_ids,
        )
        # 3b. Decayed extension: slots just outside the past boundary are folded
        #     in with a linearly decreasing weight (smooths edge effects).
        error_baseline, decayed_past_ctx = self._augment_error_baseline_with_decayed_past(
            current_slot=current_slot,
            error_baseline=error_baseline,
            exclude_request_ids=movable_future_ids,
        )
        solve_context.update(decayed_past_ctx)
        # 3c. Virtual prehistory: for startup (current_slot < W_past) synthesise
        #     missing past slots with expected arrival rate and configurable error.
        error_baseline, prehistory_ctx = self._augment_error_baseline_with_virtual_past(
            current_slot=current_slot,
            error_baseline=error_baseline,
        )
        solve_context.update(prehistory_ctx)
        # 3d. Infeasibility recovery: optionally inject "mock" low-error requests
        #     to dilute the baseline, making the window constraint feasible.
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

        # ── Step 4: global error constraint ───────────────────────────────────
        # Global error constraint: if global avg > threshold, filter flavours.
        global_stats = self.shared_state.get_global_error_stats()
        global_avg_before = global_stats["avg"]
        global_count_before = int(global_stats["count"])
        global_constraint_active = False
        _gc_enabled = bool(config.GLOBAL_ERROR_CONSTRAINT_ENABLED)
        _gc_hard = bool(config.GLOBAL_ERROR_CONSTRAINT_HARD)
        solve_context["global_error_before"] = global_avg_before
        solve_context["global_error_count_before"] = global_count_before
        if _gc_enabled and global_count_before > 0 and global_avg_before > float(config.MAX_ERROR_THRESHOLD):
            global_constraint_active = True
            if _gc_hard:
                allowed = [
                    s for s in solver.flavours
                    if float(s.get("error", 0.0)) <= float(config.MAX_ERROR_THRESHOLD)
                ]
                if allowed:
                    solver.flavours = allowed
            if config.VERBOSE:
                mode_label = "(HARD)" if _gc_hard else "(soft)"
                print(
                    f"[Scheduler] ⚠ Global error constraint {mode_label} active: "
                    f"global_avg={global_avg_before:.4f}% > threshold={config.MAX_ERROR_THRESHOLD:.2f}%"
                )
        solve_context["global_error_constraint_active"] = global_constraint_active

        # ── Step 5: DP solve (+ relaxed retry on infeasibility) ───────────────
        try:
            dp_assignments = solver.solve_batch(
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
            dp_assignments = solver._greedy_fallback(
                requests=dp_requests,
                deadlines=[max(current_slot, min(r["deadline_slot"], config.TOTAL_SLOTS - 1)) for r in dp_requests],
                current_slot=current_slot,
                capacity_tiers=config.CAPACITY_TIERS,
            )

        scheduled_pending_ids = {a.request_id for a in dp_assignments if a.request_id in pending_ids}
        if len(scheduled_pending_ids) != len(pending_ids):
            if config.VERBOSE:
                print("[Scheduler] ⚠ Infeasible with strict error window: retry with relaxed window.")

            # 5a. Relaxed retry: re-run DP without the error-window constraint
            #     (or with min-error flavour only) to try to cover all pending ids.
            relaxed_assignments, relaxed_mode = self._solve_relaxed_retry(
                solver=solver,
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
            else:
                # 5b. Greedy fallback: all pending requests are scheduled to their
                #     earliest feasible slot ignoring the error constraint entirely.
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
                dp_assignments = solver._greedy_fallback(
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
        
        # ── Step 6: convert RequestAssignment → Assignment ────────────────────
        # Convert RequestAssignment objects to Assignment objects
        assignments = []
        for dp_assignment in dp_assignments:
            metadata = assignment_metadata.get(dp_assignment.request_id, {})
            flavour_duration = self.flavour_duration_by_name.get(dp_assignment.flavour_name, 0)
            assignment = Assignment(
                request_id=dp_assignment.request_id,
                scheduled_slot=dp_assignment.slot,
                flavour_name=dp_assignment.flavour_name,
                carbon_cost=dp_assignment.carbon_cost,
                error=dp_assignment.error,
                flavour_duration=flavour_duration,
                arrival_slot=metadata.get("arrival_slot"),
                deadline_slot=metadata.get("deadline_slot"),
            )
            assignments.append(assignment)

        window_start, window_end = self._error_window_bounds(current_slot)
        solve_context["window_start_slot"] = window_start
        solve_context["window_end_slot"] = window_end
        modeled_error_sum = float(error_baseline.get("error_sum", 0.0))
        modeled_request_count = float(error_baseline.get("request_count", 0.0))
        initial_mock_count = int(dynamic_mock_pool.get("initial_count", 0))
        mock_remaining = initial_mock_count
        mock_error = float(dynamic_mock_pool.get("error_per_request", 0.0))
        for assignment in assignments:
            if window_start <= int(assignment.scheduled_slot) <= window_end:
                modeled_error_sum += float(assignment.error)
                modeled_request_count += 1.0
                if mock_remaining > 0 and mock_error > 0.0:
                    modeled_error_sum -= mock_error
                    modeled_request_count = max(0.0, modeled_request_count - 1.0)
                    mock_remaining -= 1
        mock_consumed = max(0, initial_mock_count - mock_remaining)
        solve_context["mock_recovery_consumed_in_run"] = mock_consumed
        solve_context["mock_recovery_remaining_after"] = self._consume_persistent_mock_pool(
            current_slot=current_slot,
            recovery_mode=solve_context.get("infeasibility_recovery_mode", "min_error_recovery"),
            consumed_count=mock_consumed,
        )
        solve_context["modeled_window_avg_after"] = (
            modeled_error_sum / modeled_request_count if modeled_request_count > 0 else 0.0
        )
        solve_context["modeled_window_request_count_after"] = modeled_request_count

        return assignments, solve_context

    def _create_batch_solver(self, pruning_mode: str) -> RollingWindowDPScheduler:
        """
        Build an isolated solver instance for one batch.

        This avoids shared mutable solver state across concurrent batch workers.
        """
        return RollingWindowDPScheduler(
            flavours=[dict(f) for f in self.dp_solver.flavours],
            carbon_forecast=list(self.dp_solver.carbon_forecast),
            window_size=int(self.dp_solver.window_size),
            pruning=str(pruning_mode),
            pruning_k=int(self.dp_solver.pruning_k),
            timeout=float(self.dp_solver.timeout),
        )

    def _get_effective_pruning_mode(self, pending_batch_size: int) -> str:
        """
        Resolve pruning mode based on configured threshold and current batch size.

        Rules:
        - DP_PRUNING_MIN_BATCH_SIZE <= 0 => pruning disabled
        - pending_batch_size < threshold => pruning disabled
        - otherwise use DP_PRUNING_METHOD
        """
        threshold = int(config.DP_PRUNING_MIN_BATCH_SIZE)
        if threshold <= 0:
            return "none"
        if int(pending_batch_size) < threshold:
            return "none"
        return str(config.DP_PRUNING_METHOD).strip().lower()

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
        base_request_count = float(error_baseline.get("request_count", 0.0))
        context = {
            "virtual_past_slots_used": 0,
            "virtual_past_requests": 0,
            "virtual_past_avg_error": config.MAX_ERROR_THRESHOLD * config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD,
        }

        if not config.PREHISTORY_USE_VIRTUAL_PAST:
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
            if config.PREHISTORY_STOCHASTIC_COUNTS:
                rng = random.Random(int(config.PREHISTORY_RANDOM_SEED) + slot_offset)
                virtual_count = max(1, int(rng.gauss(expected_rate, sigma)))
            else:
                virtual_count = max(1, int(round(expected_rate)))
            virtual_requests += virtual_count

        new_error_sum = base_error_sum + (virtual_requests * virtual_avg_error)
        new_request_count = base_request_count + float(virtual_requests)
        augmented = self._make_error_baseline(new_error_sum, new_request_count)
        context.update(
            {
                "virtual_past_slots_used": missing_past_slots,
                "virtual_past_requests": virtual_requests,
                "virtual_past_avg_error": virtual_avg_error,
            }
        )
        return augmented, context

    def _augment_error_baseline_with_decayed_past(
        self,
        current_slot: int,
        error_baseline: Dict[str, float],
        exclude_request_ids: Optional[Set[int]] = None,
    ) -> Tuple[Dict[str, float], Dict]:
        """
        Extend past horizon with decayed influence on older slots.

        Slots added:
            [current_slot - ERROR_WINDOW_PAST - 1, ..., current_slot - ERROR_WINDOW_PAST - K]
        where K = ERROR_WINDOW_PAST_DECAY_SLOTS.

        Weight for i-th additional slot (i=1 nearest, i=K farthest):
            (K - i + 1) / (K + 1)
        """
        base_error_sum = float(error_baseline.get("error_sum", 0.0))
        base_request_count = float(error_baseline.get("request_count", 0.0))
        decay_slots = max(0, int(config.ERROR_WINDOW_PAST_DECAY_SLOTS))
        context = {
            "decayed_past_slots_configured": decay_slots,
            "decayed_past_slots_used": 0,
            "decayed_past_weighted_requests": 0.0,
            "decayed_past_weighted_error_sum": 0.0,
        }
        if decay_slots <= 0:
            return error_baseline, context

        excluded = exclude_request_ids or set()
        weighted_request_count = 0.0
        weighted_error_sum = 0.0
        used_slots = 0
        denominator = float(decay_slots + 1)

        for idx in range(1, decay_slots + 1):
            slot = int(current_slot) - int(config.ERROR_WINDOW_PAST) - idx
            slot_assignments = [
                a
                for a in self.shared_state.get_requests_in_slot(slot)
                if a.request_id not in excluded
            ]
            slot_count = len(slot_assignments)
            if slot_count <= 0:
                continue

            slot_avg_error = sum(float(a.error) for a in slot_assignments) / float(slot_count)
            weight = float(decay_slots - idx + 1) / denominator
            slot_weighted_count = float(slot_count) * weight
            weighted_request_count += slot_weighted_count
            weighted_error_sum += slot_avg_error * slot_weighted_count
            used_slots += 1

        if weighted_request_count <= 0.0:
            return error_baseline, context

        augmented_error_sum = base_error_sum + weighted_error_sum
        augmented_request_count = base_request_count + weighted_request_count
        context.update(
            {
                "decayed_past_slots_used": used_slots,
                "decayed_past_weighted_requests": weighted_request_count,
                "decayed_past_weighted_error_sum": weighted_error_sum,
            }
        )
        return self._make_error_baseline(augmented_error_sum, augmented_request_count), context

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
        mode = str(config.INFEASIBILITY_RECOVERY_MODE).strip().lower()
        baseline_avg_error = float(error_baseline.get("average_error", 0.0))
        self._update_mock_influence_for_slot(
            current_slot=current_slot,
            baseline_avg_error=baseline_avg_error,
        )
        context = {
            "infeasibility_recovery_mode": mode,
            "mock_recovery_count": 0,
            "mock_recovery_error": 0.0,
            "mock_recovery_remaining_before": 0,
            "mock_recovery_source": "none",
            "mock_influence_base": self._mock_influence_base,
            "mock_influence_effective": self._mock_influence_effective,
            "mock_influence_decay_step": self._get_mock_influence_decay_step(),
            "mock_influence_above_threshold_streak": self._mock_influence_above_threshold_streak,
            "mock_influence_baseline_avg_error": baseline_avg_error,
        }
        dynamic_mock_pool = {"initial_count": 0, "error_per_request": 0.0}

        if mode == "min_error_recovery":
            self._reset_persistent_mock_pool()
            return error_baseline, dynamic_mock_pool, context

        augmented = dict(error_baseline)
        mock_count, mock_error, source = self._get_persistent_mock_pool(
            current_slot=current_slot,
            mode=mode,
        )
        context["mock_recovery_source"] = source
        context["mock_recovery_remaining_before"] = mock_count

        if mock_count > 0 and mock_error > 0.0:
            augmented_error_sum = float(augmented.get("error_sum", 0.0)) + mock_count * mock_error
            augmented_request_count = float(augmented.get("request_count", 0.0)) + float(mock_count)
            augmented = self._make_error_baseline(augmented_error_sum, augmented_request_count)
            dynamic_mock_pool = {"initial_count": mock_count, "error_per_request": mock_error}
            context.update(
                {
                    "mock_recovery_count": mock_count,
                    "mock_recovery_error": mock_error,
                }
            )

        return augmented, dynamic_mock_pool, context

    def _compute_mock_seed_for_mode(self, current_slot: int, mode: str) -> Tuple[int, float]:
        mock_count = 0
        mock_error = 0.0

        if mode == "carryover_last_slot":
            window_start, _ = self._error_window_bounds(current_slot)
            dropped_slot = window_start - 1
            if dropped_slot >= 0:
                dropped_assignments = self.shared_state.get_requests_in_slot(dropped_slot)
                mock_count = len(dropped_assignments)
                if mock_count > 0:
                    carryover_avg_error = sum(a.error for a in dropped_assignments) / mock_count
                    mock_error = self._resolve_infeasibility_mock_error(carryover_avg_error)

        elif mode == "forecast_mock_current_slot":
            expected_rate = float(config.PREDICTED_REQUESTS_PER_SLOT)
            sigma = max(1.0, expected_rate * float(config.REQUEST_RATE_STD_FACTOR))
            rng = random.Random(int(config.PREHISTORY_RANDOM_SEED) + int(current_slot))
            mock_count = max(0, int(rng.gauss(expected_rate, sigma)))
            forecast_default_error = (
                float(config.MAX_ERROR_THRESHOLD)
                * float(config.FORECAST_ERROR_RATIO_OF_THRESHOLD)
            )
            mock_error = self._resolve_infeasibility_mock_error(forecast_default_error)

        if mode in {"carryover_last_slot", "forecast_mock_current_slot"} and mock_count > 0:
            influence = self._mock_influence_effective
            mock_count = int(round(mock_count * influence))

        return mock_count, mock_error

    def _get_persistent_mock_pool(
        self,
        current_slot: int,
        mode: str,
    ) -> Tuple[int, float, str]:
        with self._lock:
            same_window = (
                self._persistent_mock_slot == int(current_slot)
                and self._persistent_mock_mode == str(mode)
            )
            if same_window:
                return (
                    int(self._persistent_mock_remaining),
                    float(self._persistent_mock_error),
                    "persistent_remaining",
                )

        mock_count, mock_error = self._compute_mock_seed_for_mode(current_slot=current_slot, mode=mode)
        with self._lock:
            self._persistent_mock_slot = int(current_slot)
            self._persistent_mock_mode = str(mode)
            self._persistent_mock_remaining = max(0, int(mock_count))
            self._persistent_mock_error = max(0.0, float(mock_error))
            return (
                int(self._persistent_mock_remaining),
                float(self._persistent_mock_error),
                "new_window_seed",
            )

    def _consume_persistent_mock_pool(
        self,
        current_slot: int,
        recovery_mode: str,
        consumed_count: int,
    ) -> int:
        if str(recovery_mode) == "min_error_recovery":
            return 0
        with self._lock:
            if (
                self._persistent_mock_slot != int(current_slot)
                or self._persistent_mock_mode != str(recovery_mode)
            ):
                return 0
            self._persistent_mock_remaining = max(
                0,
                int(self._persistent_mock_remaining) - max(0, int(consumed_count)),
            )
            return int(self._persistent_mock_remaining)

    def _reset_persistent_mock_pool(self) -> None:
        with self._lock:
            self._persistent_mock_slot = None
            self._persistent_mock_mode = None
            self._persistent_mock_remaining = 0
            self._persistent_mock_error = 0.0

    @staticmethod
    def _make_error_baseline(error_sum: float, request_count: float) -> Dict[str, float]:
        return {
            "error_sum": error_sum,
            "request_count": request_count,
            "average_error": error_sum / request_count if request_count > 0 else 0.0,
        }

    def _error_window_bounds(self, current_slot: int) -> Tuple[int, int]:
        return (
            max(0, current_slot - int(config.ERROR_WINDOW_PAST)),
            min(int(config.TOTAL_SLOTS) - 1, current_slot + int(config.ERROR_WINDOW_FUTURE)),
        )

    def _clamp_mock_influence(self, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _resolve_infeasibility_mock_error(self, fallback_error: float) -> float:
        configured = config.INFEASIBILITY_MOCK_ERROR_PER_REQUEST
        if configured is None:
            return max(0.0, float(fallback_error))
        return max(0.0, float(configured))

    def _get_mock_influence_decay_step(self) -> float:
        return max(
            0.0,
            float(config.INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP),
        )

    def _update_mock_influence_for_slot(self, current_slot: int, baseline_avg_error: float) -> None:
        """
        Update effective mock influence once per slot.

        Rules:
        - If baseline window error at slot start is above threshold, decay by step.
        - Decay accumulates across consecutive above-threshold slots.
        - If baseline error is under/equal threshold, reset to configured base value.
        """
        slot = int(current_slot)
        if self._mock_influence_last_eval_slot == slot:
            return

        self._mock_influence_base = self._clamp_mock_influence(
            config.INFEASIBILITY_MOCK_INFLUENCE
        )
        decay_step = self._get_mock_influence_decay_step()
        threshold = float(config.MAX_ERROR_THRESHOLD)

        if float(baseline_avg_error) > threshold:
            self._mock_influence_above_threshold_streak += 1
            self._mock_influence_effective = max(
                0.0,
                self._mock_influence_base - (self._mock_influence_above_threshold_streak * decay_step),
            )
        else:
            self._mock_influence_above_threshold_streak = 0
            self._mock_influence_effective = self._mock_influence_base

        self._mock_influence_last_eval_slot = slot
    
    def _get_carbon_forecast(self) -> List[float]:
        """
        Generate carbon intensity forecast for all time slots.
        Uses sinusoidal pattern (realistic day-night cycle).
        
        Returns:
            List of carbon intensity values [0..TOTAL_SLOTS-1]
        """
        forecast = []
        num_slots = config.TOTAL_SLOTS
        base_carbon = 250
        amplitude = 200
        
        K = 6

        for slot in range(num_slots):
            # Cycle over K slots to produce a repeating day-night pattern.
            phase = 2 * math.pi * (slot % K) / K
            value = base_carbon + amplitude * (1 + 0.8 * math.cos(phase))
            forecast.append(max(100, value))
        
        return forecast

    def get_statistics(self) -> Dict:
        """Get scheduler statistics"""
        with self._lock:
            avg_solver_ms_per_batch = self._solver_total_time_ms / self._solver_total_runs if self._solver_total_runs else 0.0
            avg_solver_ms_per_request = self._solver_total_time_ms / self._solver_total_requests if self._solver_total_requests else 0.0
            active_batch_workers = len(self._active_batch_workers)
            return {
                "batches_processed": self._batches_processed,
                "total_scheduled": self._total_scheduled,
                "solver_runs": self._solver_total_runs,
                "last_solver_elapsed_ms": self._last_solver_elapsed_ms,
                "avg_solver_ms_per_batch": avg_solver_ms_per_batch,
                "avg_solver_ms_per_request": avg_solver_ms_per_request,
                "active_batch_workers": active_batch_workers,
                "max_batch_parallelism": self._get_max_batch_parallelism(),
            }

    def _get_capacity_tier_info(self, request_count: int):
        for tier in config.CAPACITY_TIERS:
            max_req = tier["max_requests"]
            if max_req is None or request_count <= max_req:
                return float(tier["multiplier"]), max_req
        last = config.CAPACITY_TIERS[-1]
        return float(last["multiplier"]), last["max_requests"]

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
            flavour_counts = defaultdict(int)
            for a in slot_assignments:
                flavour_counts[a.flavour_name] += 1
            flavour_breakdown = "|".join(
                f"{f}:{count}" for f, count in sorted(flavour_counts.items())
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
                    "flavour_breakdown": flavour_breakdown,
                }
            )
        return rows

    def _solve_relaxed_retry(
        self,
        solver: RollingWindowDPScheduler,
        dp_requests: List[Dict],
        current_slot: int,
        baseline_slot_counts: Dict[int, int],
        baseline_slot_durations: Dict[int, int],
        error_baseline: Dict[str, float],
        assignment_cap_slot: int,
        dynamic_mock_pool: Dict[str, float],
        recovery_mode: str,
    ) -> Tuple[List, str]:
        """
        Re-run DP without the hard error-window constraint.

        Two sub-modes depending on config and recovery_mode:
        - dp_relaxed_min_error: restrict flavours to min-error only.
        - dp_relaxed_error: allow all flavours, omit max_error_threshold.
        Both restore the original flavour list on the solver before returning.
        """
        # If relaxed retry is disabled, or recovery mode explicitly requests
        # min-error recovery semantics, skip relaxed DP and force greedy.
        if (
            not config.DP_ALLOW_RELAXED_ERROR_RETRY
            or recovery_mode == "min_error_recovery"
        ):
            return [], "dp_relaxed_disabled"

        preferred_mode = "dp_relaxed_error"
        original_strategies = solver.flavours
        relaxed_flavours = original_strategies

        prefer_min_error = (
            recovery_mode == "min_error_recovery"
            or config.DP_RELAXED_RETRY_PREFER_MIN_ERROR
        )
        if prefer_min_error and original_strategies:
            min_error = min(float(s["error"]) for s in original_strategies)
            relaxed_flavours = [
                s for s in original_strategies if abs(float(s["error"]) - min_error) < 1e-9
            ]
            if relaxed_flavours:
                preferred_mode = "dp_relaxed_min_error"

        try:
            solver.flavours = relaxed_flavours
            relaxed_assignments = solver.solve_batch(
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
            solver.flavours = original_strategies

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
                    "flavour_name": assignment.flavour_name,
                    "flavour_duration": assignment.flavour_duration,
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
        if not config.ENABLE_INFEASIBILITY_DEBUG_LOGGING:
            return ""

        min_flavour_error = min((s.error for s in self.flavours), default=0.0)
        max_flavour_error = max((s.error for s in self.flavours), default=0.0)

        baseline_error_sum = float(error_baseline.get("error_sum", 0.0))
        baseline_request_count = float(error_baseline.get("request_count", 0.0))
        baseline_average_error = (
            baseline_error_sum / baseline_request_count if baseline_request_count > 0 else 0.0
        )

        pending_count = len(pending_requests)
        denominator = baseline_request_count + float(pending_count)
        min_possible_avg = (
            (baseline_error_sum + pending_count * min_flavour_error) / denominator
            if denominator > 0
            else 0.0
        )
        max_possible_avg = (
            (baseline_error_sum + pending_count * max_flavour_error) / denominator
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
            f"{a.request_id}:{a.scheduled_slot}:{a.deadline_slot if a.deadline_slot is not None else ''}:{a.flavour_name}"
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
            "min_flavour_error": min_flavour_error,
            "max_flavour_error": max_flavour_error,
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
