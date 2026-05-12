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
        "--use-config-defaults",
        action="store_true",
        help="If set, use default values from config.py instead of CLI hardcoded defaults.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for generated scenario JSON. If not specified, defaults to scenario_seed_<SEED>.json.",
    )
    
    # Parser will set defaults after checking --use-config-defaults flag
    # We do this by reading sys.argv first and checking for the flag
    use_config = "--use-config-defaults" in (sys.argv if len(sys.argv) > 1 else [])
    
    defaults_dict = {
        "seed": 2026,
        "total_slots": int(getattr(config, "TOTAL_SLOTS", 24)),
        "slot_duration_seconds": float(getattr(config, "SLOT_DURATION_SECONDS", 10.0)),
        "requests_per_slot": float(getattr(config, "PREDICTED_REQUESTS_PER_SLOT", 8.0)),
        "request_rate_std_factor": float(getattr(config, "REQUEST_RATE_STD_FACTOR", 0.35)),
        "deadline_min_slack": 0,
        "deadline_max_slack": int(getattr(config, "ASSIGNMENT_MAX_FUTURE_SLOTS", 8)),
        "error_window_past": int(getattr(config, "ERROR_WINDOW_PAST", 5)),
        "error_window_future": int(getattr(config, "ERROR_WINDOW_FUTURE", 8)),
        "max_error_threshold": float(getattr(config, "MAX_ERROR_THRESHOLD", 4.0)),
        "prehistory_error_ratio": 0.75,
        "carbon_random_noise_amplitude": 120.0,
        "prehistory_mock_influence": float(getattr(config, "PREHISTORY_MOCK_INFLUENCE", 1.0)),
    }
    
    if not use_config:
        defaults_dict.update({
            "total_slots": 24,
            "slot_duration_seconds": 10.0,
            "requests_per_slot": 8.0,
            "request_rate_std_factor": 0.35,
            "deadline_max_slack": 8,
            "error_window_past": 5,
            "error_window_future": 8,
            "max_error_threshold": 4.0,
        })
    
    parser.add_argument("--seed", type=int, default=defaults_dict["seed"])
    parser.add_argument("--total-slots", type=int, default=defaults_dict["total_slots"])
    parser.add_argument("--slot-duration-seconds", type=float, default=defaults_dict["slot_duration_seconds"])
    parser.add_argument("--requests-per-slot", type=float, default=defaults_dict["requests_per_slot"])
    parser.add_argument("--request-rate-std-factor", type=float, default=defaults_dict["request_rate_std_factor"])
    parser.add_argument("--deadline-min-slack", type=int, default=defaults_dict["deadline_min_slack"])
    parser.add_argument("--deadline-max-slack", type=int, default=defaults_dict["deadline_max_slack"])
    parser.add_argument("--error-window-past", type=int, default=defaults_dict["error_window_past"])
    parser.add_argument("--error-window-future", type=int, default=defaults_dict["error_window_future"])
    parser.add_argument("--max-error-threshold", type=float, default=defaults_dict["max_error_threshold"])
    parser.add_argument("--prehistory-error-ratio", type=float, default=defaults_dict["prehistory_error_ratio"])
    parser.add_argument(
        "--carbon-random-noise-amplitude",
        type=float,
        default=defaults_dict["carbon_random_noise_amplitude"],
        help="Uniform random amplitude added to carbon intensity (+/- value).",
    )
    parser.add_argument(
        "--prehistory-mock-influence",
        type=float,
        default=defaults_dict["prehistory_mock_influence"],
        help="Scale factor [0..1] applied to synthetic prehistory request counts.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    
    # If output not specified, generate default path using the provided seed
    if args.output is None:
        args.output = Path(__file__).resolve().parent / f"scenario_seed_{args.seed}.json"

    print("=" * 70)
    print("Scenario Generation Parameters")
    print("=" * 70)
    print(f"  Seed:                              {args.seed}")
    print(f"  Total Slots:                       {args.total_slots}")
    print(f"  Slot Duration (seconds):           {args.slot_duration_seconds}")
    print(f"  Requests per Slot:                 {args.requests_per_slot}")
    print(f"  Request Rate Std Factor:           {args.request_rate_std_factor}")
    print(f"  Deadline Min Slack (slots):        {args.deadline_min_slack}")
    print(f"  Deadline Max Slack (slots):        {args.deadline_max_slack}")
    print(f"  Error Window Past (slots):         {args.error_window_past}")
    print(f"  Error Window Future (slots):       {args.error_window_future}")
    print(f"  Max Error Threshold (%):           {args.max_error_threshold}")
    print(f"  Prehistory Error Ratio:            {args.prehistory_error_ratio}")
    print(f"  Carbon Random Noise Amplitude:     {args.carbon_random_noise_amplitude}")
    print(f"  Prehistory Mock Influence:         {args.prehistory_mock_influence}")
    print(f"  Use Config Defaults:               {args.use_config_defaults}")
    print(f"  Output Path:                       {args.output}")
    print("=" * 70)

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
    
    print(f"\nScenario saved to {args.output}")
    print(f"  Total Requests:                    {len(scenario['requests'])}")
    print(f"  Total Slots:                       {scenario['metadata']['total_slots']}")
    print(f"  Avg Requests/Slot:                 {len(scenario['requests']) / scenario['metadata']['total_slots']:.1f}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
