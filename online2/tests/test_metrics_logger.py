import csv
import tempfile
import unittest
from pathlib import Path

from metrics_logger import SolverMetricsLogger


class TestSolverMetricsLogger(unittest.TestCase):
    def test_logger_creates_and_appends_csv_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_file = str(base / "runs.csv")
            assignments_file = str(base / "assignments.csv")
            slot_file = str(base / "slots.csv")

            logger = SolverMetricsLogger(
                enabled=True,
                runs_file=runs_file,
                assignments_file=assignments_file,
                slot_metrics_file=slot_file,
            )

            run_id = logger.log_solver_run(
                run_data={
                    "current_slot": 3,
                    "pending_batch_size": 2,
                    "total_assignments": 2,
                    "new_assignments": 2,
                    "replanned_assignments": 0,
                    "solver_status": "ok",
                    "solver_mode": "dp",
                    "lock_future_assignments": True,
                    "solver_start_ts": 100.0,
                    "solver_end_ts": 100.2,
                    "solver_elapsed_ms": 200.0,
                    "avg_ms_per_new_request": 100.0,
                    "avg_ms_per_assignment": 100.0,
                    "batches_processed_after": 1,
                    "total_scheduled_after": 2,
                },
                assignment_rows=[
                    {
                        "current_slot": 3,
                        "solver_start_ts": 100.0,
                        "solver_end_ts": 100.2,
                        "request_id": 1,
                        "is_pending_request": True,
                        "scheduled_slot": 3,
                        "strategy_name": "Fast",
                        "strategy_duration": 30,
                        "error": 5.0,
                        "carbon_cost": 10.0,
                        "arrival_slot": 3,
                        "deadline_slot": 5,
                    }
                ],
                slot_metric_rows=[
                    {
                        "current_slot": 3,
                        "scheduled_slot": 3,
                        "run_slot_count": 1,
                        "total_slot_count_after": 1,
                        "avg_error_in_slot": 5.0,
                        "capacity_multiplier_after": 1.0,
                        "capacity_level_max_requests": 10,
                        "request_ids": "1",
                        "strategy_breakdown": "Fast:1",
                    }
                ],
            )

            self.assertTrue(run_id)
            self.assertTrue(Path(runs_file).exists())
            self.assertTrue(Path(assignments_file).exists())
            self.assertTrue(Path(slot_file).exists())

            with open(runs_file, newline="") as f:
                rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["solver_status"], "ok")

            with open(assignments_file, newline="") as f:
                rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["run_id"], run_id)
                self.assertEqual(rows[0]["strategy_name"], "Fast")

            with open(slot_file, newline="") as f:
                rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["run_id"], run_id)
                self.assertEqual(rows[0]["strategy_breakdown"], "Fast:1")


if __name__ == "__main__":
    unittest.main()
