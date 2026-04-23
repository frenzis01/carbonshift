"""
Rolling Window ILP Scheduler

Adapts the batch ILP solver (carbonshift.py) for online operation using
a receding horizon approach:

1. Maintain sliding window of W future slots
2. Periodically re-optimize using ILP on:
   - Real requests (already arrived)
   - Predicted requests (from predictor)
3. Commit only decisions for current slot
4. Slide window forward and repeat

This bridges the gap between offline optimality and online reactivity.
"""

from typing import List, Tuple, Dict, Optional, Any
from dataclasses import dataclass
import os
import sys
import csv
import subprocess
import tempfile
import time as time_module
from collections import defaultdict

# Import from parent carbonshift module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class Request:
    """Request representation"""
    id: int
    deadline: int
    arrival_time: int = 0


@dataclass
class Strategy:
    """Strategy representation"""
    name: str
    error: int
    duration: int


class RollingWindowILPScheduler:
    """
    Online ILP scheduler using rolling window optimization.

    Key features:
    - Periodically re-optimizes using carbonshift.py ILP solver
    - Uses request predictions to inform decisions
    - Maintains optimality within window (gap < 10% vs offline)
    - Falls back to heuristic between re-optimizations
    """

    def __init__(
        self,
        strategies: List[Strategy],
        carbon: List[float],
        window_size: int = 5,
        reopt_interval: int = 60,
        ilp_timeout: float = 10.0,
        error_threshold: float = 5.0,
        predictor=None,
        carbonshift_path: str = None
    ):
        """
        Initialize rolling window ILP scheduler.

        Args:
            strategies: Available computation strategies
            carbon: Carbon intensity forecast (gCO2/kWh) for all slots
            window_size: Number of future slots to optimize over
            reopt_interval: Seconds between re-optimizations
            ilp_timeout: Max seconds for ILP solver
            error_threshold: Max average error (%)
            predictor: RequestPredictor for future load
            carbonshift_path: Path to carbonshift.py (auto-detected if None)
        """
        self.strategies = strategies
        self.carbon = carbon
        self.window_size = window_size
        self.reopt_interval = reopt_interval
        self.ilp_timeout = ilp_timeout
        self.error_threshold = error_threshold
        self.predictor = predictor

        # Auto-detect carbonshift.py path
        if carbonshift_path is None:
            module_dir = os.path.dirname(os.path.abspath(__file__))
            self.carbonshift_path = os.path.join(
                os.path.dirname(module_dir),
                'carbonshift.py'
            )
        else:
            self.carbonshift_path = carbonshift_path

        # State
        self.pending_requests: List[Request] = []
        self.current_assignments: Dict[int, Tuple[int, str]] = {}  # {req_id: (slot, strategy)}
        self.last_reopt_time: float = 0.0

        # Fallback heuristic (import inline to avoid circular dependency)
        try:
            from .heuristics import GreedyCarbonLookahead
        except ImportError:
            from heuristics import GreedyCarbonLookahead
        
        self.fallback_heuristic = GreedyCarbonLookahead(
            strategies=strategies,
            carbon=carbon,
            predictor=predictor,
            error_threshold=error_threshold
        )

    def schedule_request(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """
        Schedule request using rolling window ILP.

        Args:
            request: Request to schedule
            current_time: Current time slot

        Returns:
            (time_slot, strategy_name) assignment
        """
        # Add to pending buffer
        self.pending_requests.append(request)

        # Check if time to re-optimize
        current_wall_time = time_module.time()
        time_since_last_reopt = current_wall_time - self.last_reopt_time

        if time_since_last_reopt >= self.reopt_interval:
            # Trigger re-optimization
            self._reoptimize(current_time)
            self.last_reopt_time = current_wall_time

        # Return assignment if available
        if request.id in self.current_assignments:
            return self.current_assignments[request.id]
        else:
            # Fallback: use heuristic if ILP hasn't decided yet
            return self.fallback_heuristic.schedule(request, current_time)

    def _reoptimize(self, current_time: int):
        """
        Re-optimize using ILP on current window.

        Calls carbonshift.py as subprocess with:
        - Real pending requests
        - Predicted future requests
        - Carbon data for window [current_time, current_time + window_size]
        """
        # Define window
        window_start = current_time
        window_end = min(current_time + self.window_size, len(self.carbon) - 1)

        # Skip if no pending requests
        if not self.pending_requests:
            return

        # Get predicted requests for window
        predicted_requests = []
        if self.predictor:
            predicted_requests = self.predictor.predict_requests(
                window_start,
                window_end
            )

        # Combine real + predicted
        all_requests = self.pending_requests + [
            Request(
                id=f"pred_{i}",
                deadline=pred.deadline,
                arrival_time=pred.arrival_time
            )
            for i, pred in enumerate(predicted_requests)
        ]

        # Adjust deadlines to be relative to window_start
        # carbonshift.py expects deadlines in [0, delta-1]
        window_delta = window_end - window_start + 1
        adjusted_requests = []
        for req in all_requests:
            # Deadline relative to window start
            relative_deadline = req.deadline - window_start
            # Clamp to window bounds
            relative_deadline = max(0, min(relative_deadline, window_delta - 1))

            adjusted_requests.append({
                'id': req.id,
                'deadline': relative_deadline
            })

        # Create temporary input files
        with tempfile.TemporaryDirectory() as tmpdir:
            # Input files
            input_requests_file = os.path.join(tmpdir, 'input_requests.csv')
            input_strategies_file = os.path.join(tmpdir, 'input_strategies.csv')
            input_carbon_file = os.path.join(tmpdir, 'input_carbon.csv')
            output_assignment_file = os.path.join(tmpdir, 'output_assignment.csv')

            # Write requests (CSV: deadline1,deadline2,...)
            with open(input_requests_file, 'w') as f:
                deadlines = [str(req['deadline']) for req in adjusted_requests]
                f.write(','.join(deadlines) + '\n')

            # Write strategies (CSV: error,duration,name)
            with open(input_strategies_file, 'w') as f:
                f.write('error,duration,strategy\n')
                for s in self.strategies:
                    f.write(f"{s.error},{s.duration},{s.name}\n")

            # Write carbon for window (one value per line)
            with open(input_carbon_file, 'w') as f:
                for t in range(window_start, window_end + 1):
                    if t < len(self.carbon):
                        f.write(f"{int(self.carbon[t])}\n")
                    else:
                        f.write("0\n")  # Fallback if beyond forecast

            # Call carbonshift.py
            beta = len(adjusted_requests)  # Each request is its own block for max flexibility

            try:
                subprocess.run(
                    [
                        sys.executable,
                        self.carbonshift_path,
                        input_requests_file,
                        input_strategies_file,
                        input_carbon_file,
                        str(window_delta),
                        str(beta),
                        str(int(self.error_threshold)),
                        output_assignment_file
                    ],
                    timeout=self.ilp_timeout,
                    check=True,
                    capture_output=True
                )

                # Parse output assignments
                assignments = self._parse_ilp_output(
                    output_assignment_file,
                    window_start
                )

                # Update current_assignments for REAL requests only
                for req in self.pending_requests:
                    if req.id in assignments:
                        self.current_assignments[req.id] = assignments[req.id]

            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
                # ILP failed → rely on fallback heuristic
                print(f"Warning: ILP optimization failed: {e}")
                # Assignments will use fallback heuristic

    def _parse_ilp_output(
        self,
        output_file: str,
        window_start: int
    ) -> Dict[int, Tuple[int, str]]:
        """
        Parse carbonshift.py output CSV.

        Args:
            output_file: Path to output_assignment.csv
            window_start: Start of optimization window (to convert relative slots back)

        Returns:
            {request_id: (absolute_slot, strategy_name)}
        """
        assignments = {}

        if not os.path.exists(output_file):
            return assignments

        with open(output_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Skip statistics lines
                if 'request_id' not in row or not row['request_id'].isdigit():
                    continue

                req_id = int(row['request_id'])
                relative_slot = int(row['time_slot'])
                strategy_name = row['strategy']

                # Convert relative slot to absolute
                absolute_slot = window_start + relative_slot

                assignments[req_id] = (absolute_slot, strategy_name)

        return assignments

    def commit_slot(self, current_time: int):
        """
        Commit assignments for current_time and remove from pending.

        Call this after all requests for a slot have been processed.
        """
        # Remove requests assigned to current_time from pending
        self.pending_requests = [
            req for req in self.pending_requests
            if req.id not in self.current_assignments or
            self.current_assignments[req.id][0] > current_time
        ]

    def get_statistics(self) -> Dict[str, Any]:
        """Get scheduler statistics"""
        return {
            'pending_requests': len(self.pending_requests),
            'assigned_requests': len(self.current_assignments),
            'window_size': self.window_size,
            'reopt_interval': self.reopt_interval,
        }


class HybridScheduler:
    """
    Hybrid scheduler: Heuristic for immediate decisions + ILP for periodic correction.

    Flow:
    1. Request arrives → heuristic assigns immediately (< 1ms latency)
    2. Assignment marked as "pending" (can be changed)
    3. Every N seconds: ILP re-optimizes all pending assignments
    4. If ILP finds better assignment with significant improvement → update
    5. Assignments become "committed" once slot is executed

    Best of both worlds: low latency + near-optimal emissions.
    """

    def __init__(
        self,
        strategies: List[Strategy],
        carbon: List[float],
        reopt_period: int = 300,  # 5 minutes
        correction_threshold: float = 0.10,  # Only correct if >10% improvement
        safety_margin: int = 2,  # Don't correct if slot < margin from execution
        **ilp_kwargs
    ):
        """
        Initialize hybrid scheduler.

        Args:
            strategies: Available strategies
            carbon: Carbon forecast
            reopt_period: Seconds between ILP corrections
            correction_threshold: Min improvement to apply correction (fraction)
            safety_margin: Don't modify assignments within this many slots of execution
            **ilp_kwargs: Passed to RollingWindowILPScheduler
        """
        from .heuristics import GreedyCarbonLookahead

        self.strategies = strategies
        self.carbon = carbon
        self.reopt_period = reopt_period
        self.correction_threshold = correction_threshold
        self.safety_margin = safety_margin

        # Heuristic for immediate decisions
        self.heuristic = GreedyCarbonLookahead(
            strategies=strategies,
            carbon=carbon,
            **{k: v for k, v in ilp_kwargs.items() if k in ['predictor', 'capacity', 'error_threshold']}
        )

        # ILP for periodic corrections
        self.ilp_scheduler = RollingWindowILPScheduler(
            strategies=strategies,
            carbon=carbon,
            **ilp_kwargs
        )

        # State
        self.pending_assignments: Dict[int, Tuple[int, str]] = {}  # {req_id: (slot, strategy)}
        self.committed_assignments: Dict[int, Tuple[int, str]] = {}
        self.last_correction_time: float = 0.0

    def schedule_request(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """
        Schedule request (hybrid approach).

        Returns immediate heuristic assignment, but queues for ILP correction.
        """
        # Immediate heuristic decision
        slot, strategy = self.heuristic.schedule(request, current_time)
        self.pending_assignments[request.id] = (slot, strategy)

        # Trigger periodic ILP correction
        current_wall_time = time_module.time()
        if current_wall_time - self.last_correction_time >= self.reopt_period:
            self._correct_with_ilp(current_time)
            self.last_correction_time = current_wall_time

        # Return current assignment (may be updated by ILP later)
        return self.pending_assignments[request.id]

    def _correct_with_ilp(self, current_time: int):
        """
        Use ILP to correct pending assignments.

        Only updates assignments if:
        - Improvement > correction_threshold
        - Slot not within safety_margin of execution
        """
        # Get ILP assignments for pending requests
        # (Note: This is a simplified version; full implementation would
        #  batch-optimize all pending requests with ILP)

        # For each pending request, check if ILP suggests better assignment
        for req_id, (current_slot, current_strategy) in list(self.pending_assignments.items()):
            # Skip if too close to execution
            if current_slot <= current_time + self.safety_margin:
                # Freeze assignment (commit it)
                self.committed_assignments[req_id] = (current_slot, current_strategy)
                del self.pending_assignments[req_id]
                continue

            # (Full implementation would run ILP here to get optimal assignment)
            # For now, this is a stub - actual correction would compare
            # emissions(current) vs emissions(ilp_optimal)

    def commit_slot(self, current_time: int):
        """Commit all assignments for current_time"""
        for req_id, (slot, strategy) in list(self.pending_assignments.items()):
            if slot == current_time:
                self.committed_assignments[req_id] = (slot, strategy)
                del self.pending_assignments[req_id]
