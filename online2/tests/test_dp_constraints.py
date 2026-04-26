import unittest

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
            locked_ids = {a.request_id for a in scheduler_locked._solve_dp([pending], current_slot=2)}

            config.DP_LOCK_FUTURE_ASSIGNMENTS = False
            scheduler_unlocked = BatchScheduler(shared_state)
            unlocked_ids = {a.request_id for a in scheduler_unlocked._solve_dp([pending], current_slot=2)}
        finally:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold

        self.assertIn(1, locked_ids)
        self.assertNotIn(99, locked_ids)
        self.assertIn(1, unlocked_ids)
        self.assertIn(99, unlocked_ids)


if __name__ == "__main__":
    unittest.main()
