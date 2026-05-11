import csv
import json
import tempfile
import unittest
from pathlib import Path

from tests.Nshift_speed.generate_scenario import generate_and_save_scenario
from tests.Nshift_speed.run_nshift_speed import run_benchmark_from_config
from tests.Nshift_speed.scenario_io import save_json


class TestRunnerOutputSchema(unittest.TestCase):
    def test_runner_generates_expected_output_files_and_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scenario_path = base / "scenario.json"
            output_dir = base / "output"
            config_path = base / "config.json"

            generate_and_save_scenario(
                output_path=scenario_path,
                seed=2028,
                total_slots=10,
                slot_duration_seconds=2.0,
                requests_per_slot=4.0,
                request_rate_std_factor=0.2,
                deadline_min_slack=0,
                deadline_max_slack=4,
                error_window_past=5,
                error_window_future=8,
                max_error_threshold=4.0,
                prehistory_error_ratio=0.75,
            )

            save_json(
                config_path,
                {
                    "batch_sizes": [2, 3],
                    "scenario_path": str(scenario_path),
                    "output_dir": str(output_dir),
                    "runner": {"flush_partial_batch": True},
                },
            )

            run_benchmark_from_config(config_path)

            summary_json = output_dir / "summary_by_n.json"
            summary_csv = output_dir / "summary_by_n.csv"
            self.assertTrue(summary_json.exists())
            self.assertTrue(summary_csv.exists())

            summary_payload = json.loads(summary_json.read_text(encoding="utf-8"))
            self.assertIn("rows", summary_payload)
            self.assertEqual(len(summary_payload["rows"]), 2)
            summary_row = summary_payload["rows"][0]
            for field in [
                "batch_size",
                "realtime_slots",
                "realtime_speed_scale",
                "solver_time_ms_min",
                "solver_time_ms_max",
                "solver_time_ms_avg",
                "queue_wait_seconds_min",
                "queue_wait_seconds_max",
                "queue_wait_seconds_avg",
                "final_wait_seconds_min",
                "final_wait_seconds_max",
                "final_wait_seconds_avg",
                "total_carbon_cost",
                "global_average_error",
                "global_average_error_real",
                "global_average_error_modeled",
            ]:
                self.assertIn(field, summary_row)

            per_request_csv = output_dir / "N2" / "per_request.csv"
            per_timeslot_csv = output_dir / "N2" / "per_timeslot.csv"
            batch_timings_csv = output_dir / "N2" / "batch_timings.csv"
            self.assertTrue(per_request_csv.exists())
            self.assertTrue(per_timeslot_csv.exists())
            self.assertTrue(batch_timings_csv.exists())

            with per_request_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                self.assertGreater(len(rows), 0)
                expected = {
                    "request_id",
                    "arrival_time",
                    "arrival_slot",
                    "deadline_slot",
                    "scheduled_slot",
                    "queue_wait_seconds",
                    "final_wait_seconds",
                }
                self.assertTrue(expected.issubset(set(rows[0].keys())))

            with per_timeslot_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                self.assertGreater(len(rows), 0)
                expected = {
                    "timeslot",
                    "window_avg_error_real",
                    "window_avg_error_modeled",
                    "real_request_count",
                    "modeled_request_count",
                }
                self.assertTrue(expected.issubset(set(rows[0].keys())))

            with batch_timings_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                self.assertGreater(len(rows), 0)
                expected = {"batch_sequence", "solver_elapsed_ms", "effective_batch_size", "flush_partial_batch"}
                self.assertTrue(expected.issubset(set(rows[0].keys())))

    def test_runner_realtime_scale_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scenario_path = base / "scenario.json"
            output_dir = base / "output"
            config_path = base / "config.json"

            generate_and_save_scenario(
                output_path=scenario_path,
                seed=2028,
                total_slots=4,
                slot_duration_seconds=1.0,
                requests_per_slot=2.0,
                request_rate_std_factor=0.2,
                deadline_min_slack=0,
                deadline_max_slack=2,
                error_window_past=2,
                error_window_future=2,
                max_error_threshold=4.0,
                prehistory_error_ratio=0.75,
            )
            save_json(
                config_path,
                {
                    "batch_sizes": [2],
                    "scenario_path": str(scenario_path),
                    "output_dir": str(output_dir),
                    "runner": {"flush_partial_batch": True},
                },
            )

            with self.assertRaises(ValueError):
                run_benchmark_from_config(config_path, realtime_speed_scale_override=1.1)


if __name__ == "__main__":
    unittest.main()
