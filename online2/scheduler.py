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
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import random

from shared_state import Request, Assignment, SharedSchedulerState
import config


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

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Statistics
        self._batches_processed = 0
        self._total_scheduled = 0

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
        current_slot = 0

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

            # Remove scheduled requests from pending
            self.shared_state.pop_pending_requests(len(assignments))

            with self._lock:
                self._batches_processed += 1
                self._total_scheduled += len(assignments)

            if config.VERBOSE:
                total_cost = sum(a.carbon_cost for a in assignments)
                avg_error = sum(a.error for a in assignments) / len(assignments)
                print(f"[Scheduler] ✓ Scheduled {len(assignments)} requests (cost={total_cost:.2f}, error={avg_error:.2f}%)")

            # Export to CSV
            self.shared_state.export_to_csv(config.OUTPUT_FILE)

    def _solve_dp(self, requests: List[Request], current_slot: int) -> List[Assignment]:
        """
        Solve batch scheduling using DP with optional Beam Search pruning.
        
        Args:
            requests: Batch of requests to schedule
            current_slot: Current slot index
            
        Returns:
            List of assignments
        """
        # TODO: Implement DP solver
        # This is a placeholder - will integrate with existing carbonshift DP module

        assignments = []

        for request in requests:
            # Naive greedy: assign to best available slot
            best_slot = None
            best_strategy = None
            best_cost = float('inf')

            for slot in range(request.arrival_slot, request.deadline_slot + 1):
                if slot >= len(config.STRATEGIES):  # Skip if beyond forecast
                    continue

                # Get carbon value for this slot (placeholder)
                carbon = 500  # TODO: Get actual carbon forecast

                for strategy in self.strategies:
                    # Calculate cost
                    cost = carbon * strategy.duration / 3600

                    # Apply capacity tier multiplier
                    cost *= self._get_capacity_multiplier(slot, len(requests))

                    if cost < best_cost:
                        best_cost = cost
                        best_slot = slot
                        best_strategy = strategy

            if best_slot is not None and best_strategy is not None:
                # Check error window constraint
                avg_error = self.shared_state.get_average_error_in_window(
                    best_slot,
                    config.ERROR_WINDOW_PAST,
                    config.ERROR_WINDOW_FUTURE
                )

                if avg_error is None or avg_error + best_strategy.error <= config.MAX_ERROR_THRESHOLD:
                    assignments.append(Assignment(
                        request_id=request.id,
                        scheduled_slot=best_slot,
                        strategy_name=best_strategy.name,
                        carbon_cost=best_cost,
                        error=best_strategy.error,
                    ))

        return assignments

    def _get_capacity_multiplier(self, slot: int, num_requests: int) -> float:
        """
        Get capacity tier multiplier for a slot based on number of scheduled requests.
        Implements rebound effect.
        
        Args:
            slot: Time slot
            num_requests: Number of requests in this batch
            
        Returns:
            Multiplier for carbon cost
        """
        # Get current assignment count for this slot
        scheduled_count = len(self.shared_state.get_requests_in_slot(slot))
        total_count = scheduled_count + num_requests

        # Find applicable tier
        for tier in config.CAPACITY_TIERS:
            if total_count <= tier["max_requests"]:
                return tier["multiplier"]

        return config.CAPACITY_TIERS[-1]["multiplier"]

    def get_statistics(self) -> Dict:
        """Get scheduler statistics"""
        with self._lock:
            return {
                "batches_processed": self._batches_processed,
                "total_scheduled": self._total_scheduled,
            }
