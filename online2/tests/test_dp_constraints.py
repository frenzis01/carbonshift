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
from tests.conftest import config_override


class TestDPSolverConstraints(unittest.TestCase):
    def test_never_schedules_before_current_slot(self):
        solver = RollingWindowDPScheduler(
            flavours=[{"name": "S", "error": 1.0, "duration": 10}],
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
            flavours=[{"name": "S", "error": 0.0, "duration": 1}],
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
            flavours=[{"name": "S", "error": 5.0, "duration": 1}],
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
            flavours=[{"name": "S", "error": 1.0, "duration": 1}],
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
            flavours=[{"name": "S", "error": 1.0, "duration": 1}],
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
                    flavour_name="Fast",
                    carbon_cost=100.0,
                    error=5.0,
                    flavour_duration=30,
                    arrival_slot=1,
                    deadline_slot=6,
                )
            ]
        )
        pending = Request(id=1, arrival_slot=2, deadline_slot=6)

        with config_override(VERBOSE=False, MAX_ERROR_THRESHOLD=10.0, DP_LOCK_FUTURE_ASSIGNMENTS=True):
            scheduler_locked = BatchScheduler(shared_state)
            locked_assignments, _ = scheduler_locked._solve_dp([pending], current_slot=2)
            locked_ids = {a.request_id for a in locked_assignments}

        with config_override(VERBOSE=False, MAX_ERROR_THRESHOLD=10.0, DP_LOCK_FUTURE_ASSIGNMENTS=False):
            scheduler_unlocked = BatchScheduler(shared_state)
            unlocked_assignments, _ = scheduler_unlocked._solve_dp([pending], current_slot=2)
            unlocked_ids = {a.request_id for a in unlocked_assignments}

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
                    flavour_name="Fast",
                    carbon_cost=100.0,
                    error=5.0,
                    flavour_duration=30,
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

        debug_rows = []
        with tempfile.TemporaryDirectory() as tmp:
            with config_override(
                DP_LOCK_FUTURE_ASSIGNMENTS=True,
                VERBOSE=False,
                MAX_ERROR_THRESHOLD=3.0,
                ENABLE_SOLVER_LOGGING=True,
                ENABLE_INFEASIBILITY_DEBUG_LOGGING=True,
                PREHISTORY_USE_VIRTUAL_PAST=False,
                DP_ALLOW_RELAXED_ERROR_RETRY=True,
                SOLVER_INFEASIBLE_DEBUG_FILE=str(Path(tmp) / "strict_debug.csv"),
            ):
                scheduler = BatchScheduler(shared_state)
                assignments, context = scheduler._solve_dp(pending_batch, current_slot=3)
                with open(config.SOLVER_INFEASIBLE_DEBUG_FILE, newline="") as f:
                    debug_rows = list(csv.DictReader(f))

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
                    flavour_name="Fast",
                    carbon_cost=100.0,
                    error=5.0,
                    flavour_duration=30,
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

        with config_override(
            DP_LOCK_FUTURE_ASSIGNMENTS=True,
            VERBOSE=False,
            MAX_ERROR_THRESHOLD=3.0,
            ENABLE_SOLVER_LOGGING=False,
            PREHISTORY_USE_VIRTUAL_PAST=False,
            ENABLE_INFEASIBILITY_DEBUG_LOGGING=False,
        ):
            cases = [
                (False, "forecast_mock_current_slot"),
                (True, "min_error_recovery"),
            ]
            for allow_relaxed_retry, recovery_mode in cases:
                with self.subTest(
                    allow_relaxed_retry=allow_relaxed_retry,
                    recovery_mode=recovery_mode,
                ):
                    with config_override(
                        DP_ALLOW_RELAXED_ERROR_RETRY=allow_relaxed_retry,
                        INFEASIBILITY_RECOVERY_MODE=recovery_mode,
                    ):
                        scheduler = BatchScheduler(shared_state)
                        assignments, context = scheduler._solve_dp(pending_batch, current_slot=3)
                        self.assertEqual(len(assignments), 3)
                        self.assertEqual(context.get("status"), "ok_greedy_after_infeasible")
                        self.assertEqual(context.get("mode"), "greedy_after_infeasible")

    def test_virtual_prehistory_baseline_is_configurable_and_applied(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(1)

        pending_batch = [
            Request(id=1, arrival_slot=1, deadline_slot=5),
            Request(id=2, arrival_slot=1, deadline_slot=5),
            Request(id=3, arrival_slot=1, deadline_slot=5),
        ]

        with config_override(
            DP_LOCK_FUTURE_ASSIGNMENTS=True,
            VERBOSE=False,
            MAX_ERROR_THRESHOLD=3.0,
            PREHISTORY_USE_VIRTUAL_PAST=True,
            PREHISTORY_STOCHASTIC_COUNTS=False,
            PREHISTORY_RANDOM_SEED=123,
            PREHISTORY_ERROR_RATIO_OF_THRESHOLD=0.5,
            PREDICTED_REQUESTS_PER_SLOT=8.0,
        ):
            scheduler = BatchScheduler(shared_state)
            assignments, context = scheduler._solve_dp(pending_batch, current_slot=1)

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

        with config_override(
            DP_PRUNING_METHOD="beam",
            DP_PRUNING_MIN_BATCH_SIZE=5,
            VERBOSE=False,
            MAX_ERROR_THRESHOLD=10.0,
            PREHISTORY_USE_VIRTUAL_PAST=False,
            DP_LOCK_FUTURE_ASSIGNMENTS=True,
        ):
            scheduler = BatchScheduler(shared_state)

            small_batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(1, 4)]
            _, small_ctx = scheduler._solve_dp(small_batch, current_slot=0)
            self.assertEqual(small_ctx.get("pruning_mode"), "none")

            large_batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(10, 15)]
            _, large_ctx = scheduler._solve_dp(large_batch, current_slot=0)
            self.assertEqual(large_ctx.get("pruning_mode"), "beam")

    def test_pruning_threshold_zero_disables_pruning(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)

        with config_override(
            DP_PRUNING_METHOD="beam",
            DP_PRUNING_MIN_BATCH_SIZE=0,
            VERBOSE=False,
            MAX_ERROR_THRESHOLD=10.0,
            PREHISTORY_USE_VIRTUAL_PAST=False,
            DP_LOCK_FUTURE_ASSIGNMENTS=True,
        ):
            scheduler = BatchScheduler(shared_state)
            batch = [Request(id=i, arrival_slot=0, deadline_slot=0) for i in range(20, 26)]
            _, solve_ctx = scheduler._solve_dp(batch, current_slot=0)
            self.assertEqual(solve_ctx.get("pruning_mode"), "none")


class TestSchedulerMockInfluenceDecay(unittest.TestCase):
    def test_mock_influence_decays_on_consecutive_above_threshold_slots_and_resets(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        with config_override(
            INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot",
            INFEASIBILITY_MOCK_INFLUENCE=0.8,
            INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP=0.10,
            MAX_ERROR_THRESHOLD=4.0,
        ):
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

        self.assertAlmostEqual(float(ctx_slot_10["mock_influence_effective"]), 0.7, places=9)
        self.assertAlmostEqual(float(ctx_slot_10_repeat["mock_influence_effective"]), 0.7, places=9)
        self.assertAlmostEqual(float(ctx_slot_11["mock_influence_effective"]), 0.6, places=9)
        self.assertAlmostEqual(float(ctx_slot_12_reset["mock_influence_effective"]), 0.8, places=9)
        self.assertAlmostEqual(float(ctx_slot_13["mock_influence_effective"]), 0.7, places=9)

    def test_mock_influence_is_clamped_to_zero(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        with config_override(
            INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot",
            INFEASIBILITY_MOCK_INFLUENCE=0.15,
            INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP=0.10,
            MAX_ERROR_THRESHOLD=4.0,
        ):
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

        self.assertAlmostEqual(float(ctx_slot_20["mock_influence_effective"]), 0.05, places=9)
        self.assertAlmostEqual(float(ctx_slot_21["mock_influence_effective"]), 0.0, places=9)
        self.assertAlmostEqual(float(ctx_slot_22["mock_influence_effective"]), 0.0, places=9)

    def test_infeasibility_mock_error_can_be_overridden_from_config(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        with config_override(
            INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot",
            INFEASIBILITY_MOCK_INFLUENCE=1.0,
            PREDICTED_REQUESTS_PER_SLOT=40.0,
            REQUEST_RATE_STD_FACTOR=0.0,
            PREHISTORY_RANDOM_SEED=4242,
            MAX_ERROR_THRESHOLD=4.0,
            FORECAST_ERROR_RATIO_OF_THRESHOLD=0.5,
            INFEASIBILITY_MOCK_ERROR_PER_REQUEST=1.23,
        ):
            _, _, ctx = scheduler._apply_infeasibility_recovery_policy(
                current_slot=5,
                error_baseline={"error_sum": 0.0, "request_count": 0, "average_error": 0.0},
            )

        self.assertGreater(int(ctx.get("mock_recovery_count", 0)), 0)
        self.assertAlmostEqual(float(ctx.get("mock_recovery_error", 0.0)), 1.23, places=9)

    def test_forecast_mock_uses_forecast_error_ratio(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        with config_override(
            INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot",
            INFEASIBILITY_MOCK_INFLUENCE=1.0,
            PREDICTED_REQUESTS_PER_SLOT=40.0,
            REQUEST_RATE_STD_FACTOR=0.0,
            PREHISTORY_RANDOM_SEED=4242,
            MAX_ERROR_THRESHOLD=4.0,
            FORECAST_ERROR_RATIO_OF_THRESHOLD=0.25,
            INFEASIBILITY_MOCK_ERROR_PER_REQUEST=None,
        ):
            _, _, ctx = scheduler._apply_infeasibility_recovery_policy(
                current_slot=5,
                error_baseline={"error_sum": 0.0, "request_count": 0, "average_error": 0.0},
            )

        self.assertGreater(int(ctx.get("mock_recovery_count", 0)), 0)
        self.assertAlmostEqual(float(ctx.get("mock_recovery_error", 0.0)), 1.0, places=9)

    def test_mock_decay_persists_across_runs_in_same_slot(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)
        scheduler = BatchScheduler(shared_state)

        with config_override(
            INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot",
            INFEASIBILITY_MOCK_INFLUENCE=1.0,
            PREDICTED_REQUESTS_PER_SLOT=40.0,
            REQUEST_RATE_STD_FACTOR=0.0,
            PREHISTORY_RANDOM_SEED=4242,
            MAX_ERROR_THRESHOLD=20.0,
            FORECAST_ERROR_RATIO_OF_THRESHOLD=0.25,
            INFEASIBILITY_MOCK_ERROR_PER_REQUEST=None,
            PREHISTORY_USE_VIRTUAL_PAST=False,
            DP_LOCK_FUTURE_ASSIGNMENTS=True,
            VERBOSE=False,
        ):
            batch_a = [
                Request(id=1001, arrival_slot=0, deadline_slot=0),
                Request(id=1002, arrival_slot=0, deadline_slot=0),
                Request(id=1003, arrival_slot=0, deadline_slot=0),
            ]
            assignments_a, ctx_a = scheduler._solve_dp(batch_a, current_slot=0)

            batch_b = [
                Request(id=1004, arrival_slot=0, deadline_slot=0),
                Request(id=1005, arrival_slot=0, deadline_slot=0),
                Request(id=1006, arrival_slot=0, deadline_slot=0),
            ]
            assignments_b, ctx_b = scheduler._solve_dp(batch_b, current_slot=0)

        self.assertEqual(len(assignments_a), 3)
        self.assertEqual(len(assignments_b), 3)
        self.assertEqual(ctx_a.get("mock_recovery_source"), "new_window_seed")
        self.assertEqual(ctx_b.get("mock_recovery_source"), "persistent_remaining")
        self.assertEqual(
            int(ctx_b.get("mock_recovery_remaining_before", 0)),
            max(0, int(ctx_a.get("mock_recovery_remaining_after", 0))),
        )
        self.assertEqual(
            int(ctx_a.get("mock_recovery_remaining_after", 0)),
            max(
                0,
                int(ctx_a.get("mock_recovery_remaining_before", 0))
                - int(ctx_a.get("mock_recovery_consumed_in_run", 0)),
            ),
        )


class TestSchedulerBatchWorkerParallelism(unittest.TestCase):
    def test_process_batch_requeues_claimed_requests_on_failure(self):
        shared_state = SharedSchedulerState()
        shared_state.set_current_slot(0)
        shared_state.add_request(Request(id=1, arrival_slot=0, deadline_slot=1))
        shared_state.add_request(Request(id=2, arrival_slot=0, deadline_slot=1))

        with config_override(BATCH_SIZE=2, VERBOSE=False):
            scheduler = BatchScheduler(shared_state)

            def fail_solve_dp(_self, requests, current_slot):
                return [], {"status": "infeasible", "mode": "dp"}

            scheduler._solve_dp = types.MethodType(fail_solve_dp, scheduler)

            scheduled = scheduler._process_batch(current_slot=0)

        self.assertFalse(scheduled)
        pending = shared_state.get_pending_requests(10)
        self.assertEqual([req.id for req in pending], [1, 2])

    def test_scheduler_respects_max_batch_parallelism(self):
        shared_state = SharedSchedulerState()
        for req_id in range(8):
            shared_state.add_request(Request(id=req_id, arrival_slot=0, deadline_slot=2))

        with config_override(
            BATCH_SIZE=1,
            SLOT_DURATION_SECONDS=1,
            MAX_BATCH_SOLVER_PARALLELISM=2,
            VERBOSE=False,
        ):
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

        self.assertEqual(shared_state.get_pending_count(), 0)
        self.assertGreaterEqual(calls, 8)
        self.assertGreaterEqual(max_active_workers, 2)
        self.assertLessEqual(max_active_workers, 2)


class TestSchedulerDecayedPastWindow(unittest.TestCase):
    def test_decayed_past_slots_extend_window_with_weighted_influence(self):
        shared_state = SharedSchedulerState()
        scheduler = BatchScheduler(shared_state)

        current_slot = 20
        # Extra past range for ERROR_WINDOW_PAST=5 and decay slots=6:
        # [14, 13, 12, 11, 10, 9] with weights [6/7, 5/7, ..., 1/7].
        old_slots = [14, 13, 12, 11, 10, 9]
        errors = [7.0, 6.0, 5.0, 4.0, 3.0, 2.0]
        for req_id, (slot, err) in enumerate(zip(old_slots, errors), start=4000):
            shared_state.add_assignments(
                [
                    Assignment(
                        request_id=req_id,
                        scheduled_slot=slot,
                        flavour_name="Balanced",
                        carbon_cost=0.0,
                        error=err,
                        flavour_duration=30,
                        arrival_slot=slot - 1,
                        deadline_slot=slot,
                    )
                ]
            )

        with config_override(ERROR_WINDOW_PAST=5, ERROR_WINDOW_PAST_DECAY_SLOTS=6):
            baseline = {"error_sum": 0.0, "request_count": 0.0, "average_error": 0.0}
            augmented, ctx = scheduler._augment_error_baseline_with_decayed_past(
                current_slot=current_slot,
                error_baseline=baseline,
                exclude_request_ids=set(),
            )

        weights = [6.0 / 7.0, 5.0 / 7.0, 4.0 / 7.0, 3.0 / 7.0, 2.0 / 7.0, 1.0 / 7.0]
        expected_count = sum(weights)
        expected_error_sum = sum(err * w for err, w in zip(errors, weights))

        self.assertEqual(int(ctx.get("decayed_past_slots_used", 0)), 6)
        self.assertAlmostEqual(float(ctx.get("decayed_past_weighted_requests", 0.0)), expected_count, places=9)
        self.assertAlmostEqual(float(augmented.get("request_count", 0.0)), expected_count, places=9)
        self.assertAlmostEqual(float(augmented.get("error_sum", 0.0)), expected_error_sum, places=9)
        self.assertAlmostEqual(
            float(augmented.get("average_error", 0.0)),
            expected_error_sum / expected_count,
            places=9,
        )


if __name__ == "__main__":
    unittest.main()
