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
from typing import List, Dict, Optional, Set
from dataclasses import dataclass

from shared_state import Request, Assignment, SharedSchedulerState
import config

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

        # Initialize DP solver
        carbon_forecast = self._get_carbon_forecast()
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
            carbon_forecast=carbon_forecast,
            window_size=config.TOTAL_SLOTS,
            pruning=config.DP_PRUNING_STRATEGY,
            pruning_k=config.DP_PRUNING_K,
            timeout=config.DP_TIMEOUT
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

                self._process_batch(slot)

            # Small sleep
            time.sleep(0.1)

    def _process_batch(self, current_slot: int) -> None:
        """
        Process a batch of pending requests.
        
        Args:
            current_slot: Current time slot
        """
        # Get requests to schedule
        pending = self.shared_state.get_pending_requests(config.BATCH_SIZE)

        if not pending:
            return

        if config.VERBOSE:
            print(f"[Scheduler] Processing {len(pending)} requests...")

        # Solve batch scheduling problem using DP
        assignments = self._solve_dp(pending, current_slot)

        if assignments:
            # Add assignments to shared state
            self.shared_state.add_assignments(assignments)

            # Remove only newly scheduled pending requests from queue.
            # Re-planned future assignments (if enabled) are not in pending.
            self.shared_state.pop_pending_requests(len(pending))

            with self._lock:
                self._batches_processed += 1
                self._total_scheduled += len(pending)

            if config.VERBOSE:
                total_cost = sum(a.carbon_cost for a in assignments)
                avg_error = sum(a.error for a in assignments) / len(assignments)
                replanned = max(0, len(assignments) - len(pending))
                print(
                    f"[Scheduler] ✓ Scheduled {len(pending)} new requests"
                    f"{' + ' + str(replanned) + ' re-planned' if replanned else ''}"
                    f" (cost={total_cost:.2f}, error={avg_error:.2f}%)"
                )

            # Export to CSV
            self.shared_state.export_to_csv(config.OUTPUT_FILE)

    def _solve_dp(self, requests: List[Request], current_slot: int) -> List[Assignment]:
        """
        Solve batch scheduling using DP with optional Beam Search pruning.
        
        Uses RollingWindowDPScheduler to find optimal batch assignment.
        
        Args:
            requests: Batch of requests to schedule
            current_slot: Current slot index
            
        Returns:
            List of assignments
        """
        pending_ids: Set[int] = {req.id for req in requests}
        pending_metadata = {
            req.id: {"arrival_slot": req.arrival_slot, "deadline_slot": req.deadline_slot}
            for req in requests
        }

        future_assignments = self.shared_state.get_future_assignments(current_slot)
        future_ids = {a.request_id for a in future_assignments}

        dp_requests = [{"id": req.id, "deadline_slot": req.deadline_slot} for req in requests]
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
                        "deadline_slot": inferred_deadline,
                    }
                )
                assignment_metadata[assignment.request_id] = {
                    "arrival_slot": assignment.arrival_slot,
                    "deadline_slot": inferred_deadline,
                }

        # Baseline load from fixed assignments that remain pinned
        baseline_slot_counts: Dict[int, int] = {}
        baseline_slot_durations: Dict[int, int] = {}
        for assignment in fixed_future_assignments:
            slot = assignment.scheduled_slot
            baseline_slot_counts[slot] = baseline_slot_counts.get(slot, 0) + 1
            duration = assignment.strategy_duration or self.strategy_duration_by_name.get(assignment.strategy_name, 0)
            baseline_slot_durations[slot] = baseline_slot_durations.get(slot, 0) + duration

        # Weighted error baseline: total error / total requests in the window
        error_baseline = self.shared_state.get_window_error_stats(
            center_slot=current_slot,
            window_past=config.ERROR_WINDOW_PAST,
            window_future=config.ERROR_WINDOW_FUTURE,
            exclude_request_ids=movable_future_ids,
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
            )
        except Exception as e:
            if config.VERBOSE:
                print(f"[Scheduler] ✗ DP solver error: {e}, falling back to greedy")
            dp_assignments = self.dp_solver._greedy_fallback(
                requests=dp_requests,
                deadlines=[max(current_slot, min(r["deadline_slot"], config.TOTAL_SLOTS - 1)) for r in dp_requests],
                current_slot=current_slot,
                capacity_tiers=config.CAPACITY_TIERS,
            )

        # Do not commit partial plans: if we fail to schedule all pending requests,
        # keep queue intact for the next iteration.
        scheduled_pending_ids = {a.request_id for a in dp_assignments if a.request_id in pending_ids}
        if len(scheduled_pending_ids) != len(pending_ids):
            if config.VERBOSE:
                print("[Scheduler] ⚠ Infeasible batch under current constraints; retrying later.")
            return []
        
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
        
        return assignments
    
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
        amplitude = 200
        
        for slot in range(num_slots):
            # Sinusoidal pattern: high at midday, low at night
            value = base_carbon + amplitude * (1 + 0.8 * (1 - abs((slot - num_slots / 2) / (num_slots / 2))))
            forecast.append(max(100, value))  # Ensure minimum carbon value
        
        return forecast

    def get_statistics(self) -> Dict:
        """Get scheduler statistics"""
        with self._lock:
            return {
                "batches_processed": self._batches_processed,
                "total_scheduled": self._total_scheduled,
            }
