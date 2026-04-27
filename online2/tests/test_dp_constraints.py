import csv
import tempfile
import unittest
from pathlib import Path

import config
from rolling_window_dp import RollingWindowDPScheduler
from scheduler import BatchScheduler
from shared_state import Assignment, Request, SharedSchedulerState


class TestDPSolverConstraints(unittest.TestCase):
    def test_never_schedules_before_current_slot(self):
        solver = RollingWindowDPScheduler(
            strategies=[{"name": "S", "error": 1.0, "duration": 10}],
            carbon_forecast=[100.0] * 8,
            window_size=8,
            pruning="beam",
            pruning_k=50,
            timeout=2.0,
        )
        assignments = solver.solve_batch(
            requests=[{"id": 1, "deadline_slot": 6}],
            current_slot=3,
        )
        self.assertEqual(len(assignments), 1)
        self.assertGreaterEqual(assignments[0].slot, 3)

    def test_capacity_tier_reprices_entire_slot(self):
        solver = RollingWindowDPScheduler(
            strategies=[{"name": "S", "error": 0.0, "duration": 1}],
            carbon_forecast=[10.0],
            window_size=1,
            pruning="none",
            timeout=2.0,
        )
        assignments = solver.solve_batch(
            requests=[{"id": "a", "deadline_slot": 0}, {"id": "b", "deadline_slot": 0}],
            current_slot=0,
            capacity_tiers=[
                {"max_requests": 1, "multiplier": 1.0},
                {"max_requests": float("inf"), "multiplier": 2.0},
            ],
        )
        self.assertEqual(len(assignments), 2)
        self.assertAlmostEqual(sum(a.carbon_cost for a in assignments), 40.0)

    def test_weighted_error_window_uses_total_error_over_total_requests(self):
        solver = RollingWindowDPScheduler(
            strategies=[{"name": "S", "error": 5.0, "duration": 1}],
            carbon_forecast=[10.0],
            window_size=1,
            pruning="none",
            timeout=2.0,
        )
        feasible = solver.solve_batch(
            requests=[{"id": 1, "deadline_slot": 0}],
            current_slot=0,
            max_error_threshold=3.0,
            error_window_baseline={"error_sum": 4.0, "request_count": 2},
        )
        infeasible = solver.solve_batch(
            requests=[{"id": 2, "deadline_slot": 0}],
            current_slot=0,
            max_error_threshold=3.0,
            error_window_baseline={"error_sum": 8.0, "request_count": 2},
        )
        self.assertEqual(len(feasible), 1)  # (4 + 5) / (2 + 1) = 3.0 -> feasible
        self.assertEqual(len(infeasible), 0)  # (8 + 5) / (2 + 1) > 3.0 -> infeasible


class TestSchedulerFutureAssignmentsFlag(unittest.TestCase):
    def test_lock_future_assignments_flag_changes_dp_scope(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(2)
        shared_state.add_assignments(
            [
                Assignment(
                    request_id=99,
                    scheduled_slot=4,
                    strategy_name="Fast",
                    carbon_cost=100.0,
                    error=5.0,
                    strategy_duration=30,
                    arrival_slot=1,
                    deadline_slot=6,
                )
            ]
        )
        pending = Request(id=1, arrival_slot=2, deadline_slot=6)

        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        original_verbose = config.VERBOSE
        original_threshold = config.MAX_ERROR_THRESHOLD
        try:
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 10.0

            config.DP_LOCK_FUTURE_ASSIGNMENTS = True
            scheduler_locked = BatchScheduler(shared_state)
            locked_assignments, _ = scheduler_locked._solve_dp([pending], current_slot=2)
            locked_ids = {a.request_id for a in locked_assignments}

            config.DP_LOCK_FUTURE_ASSIGNMENTS = False
            scheduler_unlocked = BatchScheduler(shared_state)
            unlocked_assignments, _ = scheduler_unlocked._solve_dp([pending], current_slot=2)
            unlocked_ids = {a.request_id for a in unlocked_assignments}
        finally:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold

        self.assertIn(1, locked_ids)
        self.assertNotIn(99, locked_ids)
        self.assertIn(1, unlocked_ids)
        self.assertIn(99, unlocked_ids)

    def test_infeasible_with_locked_future_falls_back_without_loop(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(3)

        # Build a baseline that makes strict window infeasible:
        # many high-error fixed future assignments in the active error window.
        high_error_assignments = []
        for req_id in range(100, 112):
            high_error_assignments.append(
                Assignment(
                    request_id=req_id,
                    scheduled_slot=3,
                    strategy_name="Fast",
                    carbon_cost=100.0,
                    error=5.0,
                    strategy_duration=30,
                    arrival_slot=2,
                    deadline_slot=6,
                )
            )
        shared_state.add_assignments(high_error_assignments)

        pending_batch = [
            Request(id=1, arrival_slot=3, deadline_slot=6),
            Request(id=2, arrival_slot=3, deadline_slot=6),
            Request(id=3, arrival_slot=3, deadline_slot=6),
        ]

        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        original_verbose = config.VERBOSE
        original_threshold = config.MAX_ERROR_THRESHOLD
        original_debug_enabled = config.ENABLE_INFEASIBILITY_DEBUG_LOGGING
        original_debug_file = config.SOLVER_INFEASIBLE_DEBUG_FILE
        original_solver_logging = config.ENABLE_SOLVER_LOGGING
        original_prehistory_use = config.PREHISTORY_USE_VIRTUAL_PAST
        debug_rows = []
        try:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 3.0
            config.ENABLE_SOLVER_LOGGING = True
            config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = True
            config.PREHISTORY_USE_VIRTUAL_PAST = False
            with tempfile.TemporaryDirectory() as tmp:
                config.SOLVER_INFEASIBLE_DEBUG_FILE = str(Path(tmp) / "strict_debug.csv")
                scheduler = BatchScheduler(shared_state)
                assignments, context = scheduler._solve_dp(pending_batch, current_slot=3)
                with open(config.SOLVER_INFEASIBLE_DEBUG_FILE, newline="") as f:
                    debug_rows = list(csv.DictReader(f))
        finally:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold
            config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = original_debug_enabled
            config.SOLVER_INFEASIBLE_DEBUG_FILE = original_debug_file
            config.ENABLE_SOLVER_LOGGING = original_solver_logging
            config.PREHISTORY_USE_VIRTUAL_PAST = original_prehistory_use

        self.assertEqual(len(assignments), 3)
        self.assertIn(context.get("mode"), {"dp_relaxed_error", "greedy_after_infeasible", "dp"})
        self.assertIn(context.get("status"), {"ok_relaxed", "ok_greedy_after_infeasible", "ok"})
        self.assertEqual(len(debug_rows), 1)
        self.assertEqual(debug_rows[0]["current_slot"], "3")

    def test_virtual_prehistory_baseline_is_configurable_and_applied(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(1)

        pending_batch = [
            Request(id=1, arrival_slot=1, deadline_slot=5),
            Request(id=2, arrival_slot=1, deadline_slot=5),
            Request(id=3, arrival_slot=1, deadline_slot=5),
        ]

        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        original_verbose = config.VERBOSE
        original_threshold = config.MAX_ERROR_THRESHOLD
        original_prehistory_use = config.PREHISTORY_USE_VIRTUAL_PAST
        original_prehistory_stochastic = config.PREHISTORY_STOCHASTIC_COUNTS
        original_prehistory_seed = config.PREHISTORY_RANDOM_SEED
        original_ratio = config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD
        original_pred_rate = config.PREDICTED_REQUESTS_PER_SLOT
        try:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 3.0
            config.PREHISTORY_USE_VIRTUAL_PAST = True
            config.PREHISTORY_STOCHASTIC_COUNTS = False
            config.PREHISTORY_RANDOM_SEED = 123
            config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD = 0.5
            config.PREDICTED_REQUESTS_PER_SLOT = 8.0

            scheduler = BatchScheduler(shared_state)
            assignments, context = scheduler._solve_dp(pending_batch, current_slot=1)
        finally:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold
            config.PREHISTORY_USE_VIRTUAL_PAST = original_prehistory_use
            config.PREHISTORY_STOCHASTIC_COUNTS = original_prehistory_stochastic
            config.PREHISTORY_RANDOM_SEED = original_prehistory_seed
            config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD = original_ratio
            config.PREDICTED_REQUESTS_PER_SLOT = original_pred_rate

        self.assertEqual(len(assignments), 3)
        self.assertEqual(context.get("virtual_past_slots_used"), config.ERROR_WINDOW_PAST - 1)
        self.assertGreater(int(context.get("virtual_past_requests", 0)), 0)
        self.assertAlmostEqual(
            float(context.get("virtual_past_avg_error", 0.0)),
            0.5 * float(config.MAX_ERROR_THRESHOLD),
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
