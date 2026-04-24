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
import sys
import os

from shared_state import Request, Assignment, SharedSchedulerState
import config

# Import DP solver
from rolling_window_dp import RollingWindowDPScheduler, RequestAssignment


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
        
        Uses RollingWindowDPScheduler to find optimal batch assignment.
        
        Args:
            requests: Batch of requests to schedule
            current_slot: Current slot index
            
        Returns:
            List of assignments
        """
        # Prepare input for DP solver
        dp_requests = []
        for req in requests:
            dp_requests.append({
                'id': req.id,
                'deadline_slot': req.deadline_slot
            })
        
        # Calculate capacity multiplier for all requests in batch
        capacity_multiplier = self._get_capacity_multiplier(current_slot, len(requests))
        
        # Solve using DP
        try:
            dp_assignments = self.dp_solver.solve_batch(
                requests=dp_requests,
                current_slot=current_slot,
                capacity_multiplier=capacity_multiplier,
                error_window_errors=None
            )
        except Exception as e:
            if config.VERBOSE:
                print(f"[Scheduler] ✗ DP solver error: {e}, falling back to greedy")
            dp_assignments = []
        
        # Convert RequestAssignment objects to Assignment objects
        assignments = []
        for dp_assignment in dp_assignments:
            assignment = Assignment(
                request_id=dp_assignment.request_id,
                scheduled_slot=dp_assignment.slot,
                strategy_name=dp_assignment.strategy_name,
                carbon_cost=dp_assignment.carbon_cost,
                error=dp_assignment.error,
            )
            assignments.append(assignment)
        
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
            # Use sin to create realistic day-night cycle
            phase = (slot / num_slots) * 2 * 3.14159
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
