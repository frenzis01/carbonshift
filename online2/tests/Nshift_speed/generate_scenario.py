"""Generate deterministic scenario JSON for N-shift speed benchmarks."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Dict, Optional

ONLINE2_ROOT = Path(__file__).resolve().parents[2]
if str(ONLINE2_ROOT) not in sys.path:
    sys.path.insert(0, str(ONLINE2_ROOT))

import config
from tests.Nshift_speed.scenario_io import generate_scenario_data, save_json


def generate_and_save_scenario(
    *,
    output_path: Path,
    seed: int,
    total_slots: int,
    slot_duration_seconds: float,
    requests_per_slot: float,
    request_rate_std_factor: float,
    deadline_min_slack: int,
    deadline_max_slack: int,
    error_window_past: int,
    error_window_future: int,
    max_error_threshold: float,
    prehistory_error_ratio: float,
    carbon_random_noise_amplitude: float = 120.0,
    prehistory_mock_influence: Optional[float] = None,
) -> Dict[str, Any]:
    if prehistory_mock_influence is None:
        prehistory_mock_influence = float(getattr(config, "PREHISTORY_MOCK_INFLUENCE", 1.0))
    scenario = generate_scenario_data(
        seed=seed,
        total_slots=total_slots,
        slot_duration_seconds=slot_duration_seconds,
        requests_per_slot=requests_per_slot,
        request_rate_std_factor=request_rate_std_factor,
        deadline_min_slack=deadline_min_slack,
        deadline_max_slack=deadline_max_slack,
        error_window_past=error_window_past,
        error_window_future=error_window_future,
        max_error_threshold=max_error_threshold,
        prehistory_error_ratio=prehistory_error_ratio,
        carbon_random_noise_amplitude=carbon_random_noise_amplitude,
        prehistory_mock_influence=prehistory_mock_influence,
    )
    save_json(output_path, scenario)
    return scenario


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic N-shift benchmark scenario.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "scenario_seed_2026.json",
        help="Path for generated scenario JSON.",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--total-slots", type=int, default=24)
    parser.add_argument("--slot-duration-seconds", type=float, default=10.0)
    parser.add_argument("--requests-per-slot", type=float, default=8.0)
    parser.add_argument("--request-rate-std-factor", type=float, default=0.35)
    parser.add_argument("--deadline-min-slack", type=int, default=0)
    parser.add_argument("--deadline-max-slack", type=int, default=8)
    parser.add_argument("--error-window-past", type=int, default=5)
    parser.add_argument("--error-window-future", type=int, default=8)
    parser.add_argument("--max-error-threshold", type=float, default=4.0)
    parser.add_argument("--prehistory-error-ratio", type=float, default=0.75)
    parser.add_argument(
        "--carbon-random-noise-amplitude",
        type=float,
        default=120.0,
        help="Uniform random amplitude added to carbon intensity (+/- value).",
    )
    parser.add_argument(
        "--prehistory-mock-influence",
        type=float,
        default=float(getattr(config, "PREHISTORY_MOCK_INFLUENCE", 1.0)),
        help="Scale factor [0..1] applied to synthetic prehistory request counts.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    scenario = generate_and_save_scenario(
        output_path=args.output,
        seed=args.seed,
        total_slots=args.total_slots,
        slot_duration_seconds=args.slot_duration_seconds,
        requests_per_slot=args.requests_per_slot,
        request_rate_std_factor=args.request_rate_std_factor,
        deadline_min_slack=args.deadline_min_slack,
        deadline_max_slack=args.deadline_max_slack,
        error_window_past=args.error_window_past,
        error_window_future=args.error_window_future,
        max_error_threshold=args.max_error_threshold,
        prehistory_error_ratio=args.prehistory_error_ratio,
        carbon_random_noise_amplitude=args.carbon_random_noise_amplitude,
        prehistory_mock_influence=args.prehistory_mock_influence,
    )
    print(
        f"Scenario saved to {args.output} "
        f"(requests={len(scenario['requests'])}, total_slots={scenario['metadata']['total_slots']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
