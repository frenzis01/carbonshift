import csv
import tempfile
import threading
import time
import types
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

    def test_strict_threshold_checks_final_window_average_even_if_new_requests_outside_window(self):
        forecast = [1000.0] * 6 + [1.0] * 6
        solver = RollingWindowDPScheduler(
            strategies=[{"name": "S", "error": 1.0, "duration": 1}],
            carbon_forecast=forecast,
            window_size=12,
            pruning="none",
            timeout=2.0,
        )
        assignments = solver.solve_batch(
            requests=[{"id": 1, "deadline_slot": 11}],
            current_slot=0,
            max_error_threshold=4.0,
            error_window_baseline={"error_sum": 50.0, "request_count": 10},  # baseline avg=5.0 (>4.0)
            error_window_past=5,
            error_window_future=5,
        )
        self.assertEqual(len(assignments), 0)

    def test_assignment_max_slot_limits_deadline_domain(self):
        solver = RollingWindowDPScheduler(
            strategies=[{"name": "S", "error": 1.0, "duration": 1}],
            carbon_forecast=[10.0] * 12,
            window_size=12,
            pruning="none",
            timeout=2.0,
        )
        assignments = solver.solve_batch(
            requests=[{"id": 1, "deadline_slot": 11}],
            current_slot=2,
            assignment_max_slot=4,
        )
        self.assertEqual(len(assignments), 1)
        self.assertLessEqual(assignments[0].slot, 4)


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
        original_relaxed_retry = config.DP_ALLOW_RELAXED_ERROR_RETRY
        debug_rows = []
        try:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 3.0
            config.ENABLE_SOLVER_LOGGING = True
            config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = True
            config.PREHISTORY_USE_VIRTUAL_PAST = False
            config.DP_ALLOW_RELAXED_ERROR_RETRY = True
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
            config.DP_ALLOW_RELAXED_ERROR_RETRY = original_relaxed_retry

        self.assertEqual(len(assignments), 3)
        self.assertIn(
            context.get("mode"),
            {"dp_relaxed_error", "dp_relaxed_min_error", "greedy_after_infeasible", "dp"},
        )
        self.assertIn(context.get("status"), {"ok_relaxed", "ok_greedy_after_infeasible", "ok"})
        self.assertEqual(len(debug_rows), 1)
        self.assertEqual(debug_rows[0]["current_slot"], "3")

    def test_strict_infeasible_forces_greedy_when_relaxed_disabled_or_min_error_mode(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(3)

        # Build a strict-infeasible baseline in the active window.
        high_error_assignments = []
        for req_id in range(200, 212):
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
            Request(id=11, arrival_slot=3, deadline_slot=6),
            Request(id=12, arrival_slot=3, deadline_slot=6),
            Request(id=13, arrival_slot=3, deadline_slot=6),
        ]

        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        original_verbose = config.VERBOSE
        original_threshold = config.MAX_ERROR_THRESHOLD
        original_solver_logging = config.ENABLE_SOLVER_LOGGING
        original_prehistory_use = config.PREHISTORY_USE_VIRTUAL_PAST
        original_relaxed_retry = config.DP_ALLOW_RELAXED_ERROR_RETRY
        original_infeasibility_mode = config.INFEASIBILITY_RECOVERY_MODE
        original_debug_enabled = config.ENABLE_INFEASIBILITY_DEBUG_LOGGING
        try:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 3.0
            config.ENABLE_SOLVER_LOGGING = False
            config.PREHISTORY_USE_VIRTUAL_PAST = False
            config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = False

            cases = [
                (False, "forecast_mock_current_slot"),
                (True, "min_error_recovery"),
            ]
            for allow_relaxed_retry, recovery_mode in cases:
                with self.subTest(
                    allow_relaxed_retry=allow_relaxed_retry,
                    recovery_mode=recovery_mode,
                ):
                    config.DP_ALLOW_RELAXED_ERROR_RETRY = allow_relaxed_retry
                    config.INFEASIBILITY_RECOVERY_MODE = recovery_mode
                    scheduler = BatchScheduler(shared_state)
                    assignments, context = scheduler._solve_dp(pending_batch, current_slot=3)
                    self.assertEqual(len(assignments), 3)
                    self.assertEqual(context.get("status"), "ok_greedy_after_infeasible")
                    self.assertEqual(context.get("mode"), "greedy_after_infeasible")
        finally:
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold
            config.ENABLE_SOLVER_LOGGING = original_solver_logging
            config.PREHISTORY_USE_VIRTUAL_PAST = original_prehistory_use
            config.DP_ALLOW_RELAXED_ERROR_RETRY = original_relaxed_retry
            config.INFEASIBILITY_RECOVERY_MODE = original_infeasibility_mode
            config.ENABLE_INFEASIBILITY_DEBUG_LOGGING = original_debug_enabled

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
            0.5 * 3.0,
            places=6,
        )


class TestSchedulerPruningThreshold(unittest.TestCase):
    def test_pruning_applies_only_at_or_above_threshold(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)

        original_strategy = config.DP_PRUNING_STRATEGY
        original_threshold = getattr(config, "DP_PRUNING_MIN_BATCH_SIZE", 0)
        original_verbose = config.VERBOSE
        original_threshold_error = config.MAX_ERROR_THRESHOLD
        original_prehistory = config.PREHISTORY_USE_VIRTUAL_PAST
        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        try:
            config.DP_PRUNING_STRATEGY = "beam"
            config.DP_PRUNING_MIN_BATCH_SIZE = 5
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 10.0
            config.PREHISTORY_USE_VIRTUAL_PAST = False
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True

            scheduler = BatchScheduler(shared_state)

            small_batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(1, 4)]
            _, small_ctx = scheduler._solve_dp(small_batch, current_slot=0)
            self.assertEqual(small_ctx.get("pruning_mode"), "none")

            large_batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(10, 15)]
            _, large_ctx = scheduler._solve_dp(large_batch, current_slot=0)
            self.assertEqual(large_ctx.get("pruning_mode"), "beam")
        finally:
            config.DP_PRUNING_STRATEGY = original_strategy
            config.DP_PRUNING_MIN_BATCH_SIZE = original_threshold
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold_error
            config.PREHISTORY_USE_VIRTUAL_PAST = original_prehistory
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock

    def test_pruning_threshold_zero_disables_pruning(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)

        original_strategy = config.DP_PRUNING_STRATEGY
        original_threshold = getattr(config, "DP_PRUNING_MIN_BATCH_SIZE", 0)
        original_verbose = config.VERBOSE
        original_threshold_error = config.MAX_ERROR_THRESHOLD
        original_prehistory = config.PREHISTORY_USE_VIRTUAL_PAST
        original_lock = config.DP_LOCK_FUTURE_ASSIGNMENTS
        try:
            config.DP_PRUNING_STRATEGY = "beam"
            config.DP_PRUNING_MIN_BATCH_SIZE = 0
            config.VERBOSE = False
            config.MAX_ERROR_THRESHOLD = 10.0
            config.PREHISTORY_USE_VIRTUAL_PAST = False
            config.DP_LOCK_FUTURE_ASSIGNMENTS = True

            scheduler = BatchScheduler(shared_state)
            batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(20, 26)]
            _, solve_ctx = scheduler._solve_dp(batch, current_slot=0)
            self.assertEqual(solve_ctx.get("pruning_mode"), "none")
        finally:
            config.DP_PRUNING_STRATEGY = original_strategy
            config.DP_PRUNING_MIN_BATCH_SIZE = original_threshold
            config.VERBOSE = original_verbose
            config.MAX_ERROR_THRESHOLD = original_threshold_error
            config.PREHISTORY_USE_VIRTUAL_PAST = original_prehistory
            config.DP_LOCK_FUTURE_ASSIGNMENTS = original_lock


class TestSchedulerMockInfluenceDecay(unittest.TestCase):
    def test_mock_influence_decays_on_consecutive_above_threshold_slots_and_resets(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        original_mode = config.INFEASIBILITY_RECOVERY_MODE
        original_influence = config.INFEASIBILITY_MOCK_INFLUENCE
        original_decay = getattr(config, "INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP", 0.10)
        original_threshold = config.MAX_ERROR_THRESHOLD
        try:
            config.INFEASIBILITY_RECOVERY_MODE = "forecast_mock_current_slot"
            config.INFEASIBILITY_MOCK_INFLUENCE = 0.8
            config.INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP = 0.10
            config.MAX_ERROR_THRESHOLD = 4.0

            _, _, ctx_slot_10 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=10,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
            _, _, ctx_slot_10_repeat = scheduler._apply_infeasibility_recovery_policy(
                current_slot=10,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
            _, _, ctx_slot_11 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=11,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
            _, _, ctx_slot_12_reset = scheduler._apply_infeasibility_recovery_policy(
                current_slot=12,
                error_baseline={"error_sum": 30.0, "request_count": 10, "average_error": 3.0},
            )
            _, _, ctx_slot_13 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=13,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
        finally:
            config.INFEASIBILITY_RECOVERY_MODE = original_mode
            config.INFEASIBILITY_MOCK_INFLUENCE = original_influence
            config.INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP = original_decay
            config.MAX_ERROR_THRESHOLD = original_threshold

        self.assertAlmostEqual(float(ctx_slot_10["mock_influence_effective"]), 0.7, places=9)
        self.assertAlmostEqual(float(ctx_slot_10_repeat["mock_influence_effective"]), 0.7, places=9)
        self.assertAlmostEqual(float(ctx_slot_11["mock_influence_effective"]), 0.6, places=9)
        self.assertAlmostEqual(float(ctx_slot_12_reset["mock_influence_effective"]), 0.8, places=9)
        self.assertAlmostEqual(float(ctx_slot_13["mock_influence_effective"]), 0.7, places=9)

    def test_mock_influence_is_clamped_to_zero(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        original_mode = config.INFEASIBILITY_RECOVERY_MODE
        original_influence = config.INFEASIBILITY_MOCK_INFLUENCE
        original_decay = getattr(config, "INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP", 0.10)
        original_threshold = config.MAX_ERROR_THRESHOLD
        try:
            config.INFEASIBILITY_RECOVERY_MODE = "forecast_mock_current_slot"
            config.INFEASIBILITY_MOCK_INFLUENCE = 0.15
            config.INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP = 0.10
            config.MAX_ERROR_THRESHOLD = 4.0

            _, _, ctx_slot_20 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=20,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
            _, _, ctx_slot_21 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=21,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
            _, _, ctx_slot_22 = scheduler._apply_infeasibility_recovery_policy(
                current_slot=22,
                error_baseline={"error_sum": 50.0, "request_count": 10, "average_error": 5.0},
            )
        finally:
            config.INFEASIBILITY_RECOVERY_MODE = original_mode
            config.INFEASIBILITY_MOCK_INFLUENCE = original_influence
            config.INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP = original_decay
            config.MAX_ERROR_THRESHOLD = original_threshold

        self.assertAlmostEqual(float(ctx_slot_20["mock_influence_effective"]), 0.05, places=9)
        self.assertAlmostEqual(float(ctx_slot_21["mock_influence_effective"]), 0.0, places=9)
        self.assertAlmostEqual(float(ctx_slot_22["mock_influence_effective"]), 0.0, places=9)


class TestSchedulerBatchWorkerParallelism(unittest.TestCase):
    def test_process_batch_requeues_claimed_requests_on_failure(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)
        shared_state.add_request(Request(id=1, arrival_slot=0, deadline_slot=1))
        shared_state.add_request(Request(id=2, arrival_slot=0, deadline_slot=1))

        original_batch_size = config.BATCH_SIZE
        original_verbose = config.VERBOSE
        try:
            config.BATCH_SIZE = 2
            config.VERBOSE = False
            scheduler = BatchScheduler(shared_state)

            def fail_solve_dp(_self, requests, current_slot):
                return [], {"status": "infeasible", "mode": "dp"}

            scheduler._solve_dp = types.MethodType(fail_solve_dp, scheduler)

            scheduled = scheduler._process_batch(current_slot=0)
        finally:
            config.BATCH_SIZE = original_batch_size
            config.VERBOSE = original_verbose

        self.assertFalse(scheduled)
        pending = shared_state.get_pending_requests(10)
        self.assertEqual([req.id for req in pending], [1, 2])

    def test_scheduler_respects_max_batch_parallelism(self):
        shared_state = SharedSchedulerState()
        for req_id in range(8):
            shared_state.add_request(Request(id=req_id, arrival_slot=0, deadline_slot=2))

        original_batch_size = config.BATCH_SIZE
        original_slot_duration = config.SLOT_DURATION_SECONDS
        original_verbose = config.VERBOSE
        original_parallel = getattr(
            config,
            "MAX_BATCH_SOLVER_PARALLELISM",
            getattr(config, "NUM_SCHEDULER_THREADS", 1),
        )
        try:
            config.BATCH_SIZE = 1
            config.SLOT_DURATION_SECONDS = 1
            config.MAX_BATCH_SOLVER_PARALLELISM = 2
            config.VERBOSE = False

            scheduler = BatchScheduler(shared_state)

            tracker_lock = threading.Lock()
            active_workers = 0
            max_active_workers = 0
            calls = 0
            done_event = threading.Event()

            def fake_process_batch(_self, current_slot, pending_override=None):
                nonlocal active_workers, max_active_workers, calls
                with tracker_lock:
                    active_workers += 1
                    max_active_workers = max(max_active_workers, active_workers)
                    calls += 1
                time.sleep(0.05)
                with tracker_lock:
                    active_workers -= 1
                    if _self.shared_state.get_pending_count() == 0:
                        done_event.set()
                return True

            scheduler._process_batch = types.MethodType(fake_process_batch, scheduler)

            scheduler.start()
            done_event.wait(timeout=3.0)
            scheduler.stop()
        finally:
            config.BATCH_SIZE = original_batch_size
            config.SLOT_DURATION_SECONDS = original_slot_duration
            config.MAX_BATCH_SOLVER_PARALLELISM = original_parallel
            config.VERBOSE = original_verbose

        self.assertEqual(shared_state.get_pending_count(), 0)
        self.assertGreaterEqual(calls, 8)
        self.assertGreaterEqual(max_active_workers, 2)
        self.assertLessEqual(max_active_workers, 2)


if __name__ == "__main__":
    unittest.main()
