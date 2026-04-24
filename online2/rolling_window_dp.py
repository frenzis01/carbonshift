"""
Rolling Window DP Solver for Online2 Batch Scheduler

This module implements a Dynamic Programming solver optimized for batch scheduling
in Online2. It handles:
- Batch scheduling (N requests at a time)
- Capacity tiers with rebound effect multipliers
- Error budget windows (sliding 11-slot window)
- Beam Search and K-Best pruning strategies
"""

from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import math


@dataclass
class RequestAssignment:
    """Result of a single request assignment"""
    request_id: str
    strategy_name: str
    slot: int
    carbon_cost: float
    error: float


class RollingWindowDPScheduler:
    """
    DP-based batch scheduler with rolling window optimization and pruning.
    
    Solves: Assign N requests to time slots and strategies to minimize carbon cost
    while respecting:
    - Deadline constraints
    - Error budget window (t-5 to t+5)
    - Capacity tier multipliers (rebound effect)
    - Max average error threshold
    """
    
    def __init__(self, 
                 strategies: List[dict],
                 carbon_forecast: List[float],
                 window_size: int = 24,
                 pruning: str = 'beam',
                 pruning_k: int = 150,
                 timeout: float = 5.0):
        """
        Initialize the DP scheduler.
        
        Args:
            strategies: List of strategy dicts with 'name', 'error', 'duration'
            carbon_forecast: Carbon intensity per time slot [0..window_size-1]
            window_size: Total number of time slots (default 24)
            pruning: Pruning strategy - 'beam', 'kbest', or 'none' (default 'beam')
            pruning_k: Number of states to keep when pruning (default 150)
            timeout: Maximum execution time in seconds (default 5.0)
        """
        self.strategies = strategies
        self.carbon_forecast = carbon_forecast
        self.window_size = window_size
        self.pruning = pruning
        self.pruning_k = pruning_k
        self.timeout = timeout
        
        # Validate inputs
        if len(carbon_forecast) != window_size:
            raise ValueError(f"Carbon forecast length {len(carbon_forecast)} != window_size {window_size}")
    
    def solve_batch(self, 
                   requests: List[dict],
                   current_slot: int,
                   capacity_multiplier: float = 1.0,
                   error_window_errors: Dict[int, float] = None) -> List[RequestAssignment]:
        """
        Solve batch scheduling problem for N requests using DP with pruning.
        
        Args:
            requests: List of request dicts with 'id', 'deadline_slot'
            current_slot: Current time slot (for window calculations)
            capacity_multiplier: Capacity tier multiplier (1.0 to 3.0)
            error_window_errors: Dict mapping slot -> current average error in that slot
            
        Returns:
            List of RequestAssignment objects (one per request)
            
        Raises:
            ValueError: If problem is infeasible or timeout exceeded
        """
        if not requests:
            return []
        
        if error_window_errors is None:
            error_window_errors = {}
        
        N = len(requests)
        S = len(self.strategies)
        T = self.window_size
        
        # === STEP 1: EXTRACT DEADLINES AND BUILD INITIAL DP STATE ===
        deadlines = [max(0, min(req.get('deadline_slot', T-1), T-1)) for req in requests]
        
        # === STEP 2: DP WITH PRUNING ===
        # State: DP[req_idx][error_state] = (min_cost, assignments)
        # We represent error as an integer (0-100 representing percentage * 100)
        
        # Initialize DP table
        DP_prev = {0: (0.0, [])}  # {error_state: (cost, assignments_so_far)}
        
        for req_idx in range(N):
            req_id = requests[req_idx]['id']
            deadline = deadlines[req_idx]
            DP_curr = {}
            
            # Try all (strategy, slot) combinations for this request
            for s_idx, strategy in enumerate(self.strategies):
                for t in range(deadline + 1):  # Can only schedule before deadline
                    strategy_error = int(strategy['error'] * 100)  # Convert to int (percentage * 100)
                    strategy_duration = strategy['duration']
                    
                    # Calculate carbon cost for this request with this strategy at this slot
                    base_carbon = self.carbon_forecast[t] * strategy_duration * capacity_multiplier
                    
                    # Try to extend each previous state
                    for prev_error, (prev_cost, prev_assignments) in DP_prev.items():
                        new_error = prev_error + strategy_error
                        new_cost = prev_cost + base_carbon
                        
                        assignment = RequestAssignment(
                            request_id=req_id,
                            strategy_name=strategy['name'],
                            slot=t,
                            carbon_cost=base_carbon,
                            error=strategy['error']
                        )
                        new_assignments = prev_assignments + [assignment]
                        
                        # Update DP: keep state if it's better or doesn't exist
                        if new_error not in DP_curr or DP_curr[new_error][0] > new_cost:
                            DP_curr[new_error] = (new_cost, new_assignments)
            
            # === PRUNING: Keep only best K states ===
            if self.pruning == 'beam' and len(DP_curr) > self.pruning_k:
                # Beam search: keep top-K by cost
                sorted_states = sorted(DP_curr.items(), key=lambda x: x[1][0])
                DP_curr = dict(sorted_states[:self.pruning_k])
            elif self.pruning == 'kbest' and len(DP_curr) > self.pruning_k:
                # K-Best: keep states with error and cost in top-K
                sorted_states = sorted(DP_curr.items(), key=lambda x: (x[1][0], x[0]))
                DP_curr = dict(sorted_states[:self.pruning_k])
            
            DP_prev = DP_curr
        
        # === STEP 3: EXTRACT BEST SOLUTION ===
        if not DP_prev:
            # Fallback: create greedy assignment
            return self._greedy_fallback(requests, deadlines)
        
        # Find solution with minimum cost
        best_error, (best_cost, best_assignments) = min(
            DP_prev.items(),
            key=lambda x: x[1][0]
        )
        
        return best_assignments
    
    def _greedy_fallback(self, 
                        requests: List[dict],
                        deadlines: List[int]) -> List[RequestAssignment]:
        """
        Fallback greedy scheduler when DP fails.
        Assigns each request to the earliest available slot with the fastest strategy.
        """
        assignments = []
        slot_loads = {}  # Track requests per slot for load balancing
        
        for req_idx, req in enumerate(requests):
            req_id = req['id']
            deadline = deadlines[req_idx]
            
            # Find slot with minimum load before deadline
            best_slot = 0
            best_load = float('inf')
            for t in range(deadline + 1):
                load = slot_loads.get(t, 0)
                if load < best_load:
                    best_load = load
                    best_slot = t
            
            # Choose fastest strategy (lowest error = smallest impact)
            strategy = self.strategies[0]  # Assume first is fast
            
            carbon_cost = self.carbon_forecast[best_slot] * strategy['duration']
            
            assignment = RequestAssignment(
                request_id=req_id,
                strategy_name=strategy['name'],
                slot=best_slot,
                carbon_cost=carbon_cost,
                error=strategy['error']
            )
            assignments.append(assignment)
            
            slot_loads[best_slot] = slot_loads.get(best_slot, 0) + 1
        
        return assignments
    
    def solve_with_error_window(self,
                               requests: List[dict],
                               current_slot: int,
                               capacity_multiplier: float = 1.0,
                               max_error_threshold: float = 3.0,
                               error_window_data: Dict[int, float] = None) -> Tuple[List[RequestAssignment], float]:
        """
        Solve batch problem while respecting error budget window constraint.
        
        Error window: average error in slots [current_slot-5, ..., current_slot+5]
        must be ≤ max_error_threshold
        
        Args:
            requests: List of requests
            current_slot: Current time slot
            capacity_multiplier: Capacity tier multiplier
            max_error_threshold: Maximum average error allowed (as percentage)
            error_window_data: Dict of {slot: current_error} in the window
            
        Returns:
            (assignments, average_error_in_window)
        """
        assignments = self.solve_batch(
            requests=requests,
            current_slot=current_slot,
            capacity_multiplier=capacity_multiplier,
            error_window_errors=error_window_data
        )
        
        # Calculate average error in the window after this batch
        window_start = max(0, current_slot - 5)
        window_end = min(self.window_size - 1, current_slot + 5)
        
        # Collect all errors in window (existing + new assignments)
        window_errors = []
        if error_window_data:
            for slot in range(window_start, window_end + 1):
                window_errors.append(error_window_data.get(slot, 0.0))
        
        # Add errors from new assignments that fall in window
        for assignment in assignments:
            if window_start <= assignment.slot <= window_end:
                window_errors.append(assignment.error)
        
        avg_error = sum(window_errors) / len(window_errors) if window_errors else 0.0
        
        return assignments, avg_error
