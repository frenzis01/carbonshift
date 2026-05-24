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
    carbon_random_noise_amplitude: float = 40.0,
    prehistory_mock_influence: Optional[float] = None,
    error_window_past_decay_slots: Optional[int] = None,
    include_prehistory: Optional[bool] = None,
) -> Dict[str, Any]:
    if prehistory_mock_influence is None:
        prehistory_mock_influence = float(config.PREHISTORY_MOCK_INFLUENCE)
    if error_window_past_decay_slots is None:
        error_window_past_decay_slots = int(config.ERROR_WINDOW_PAST_DECAY_SLOTS)
    if include_prehistory is None:
        include_prehistory = True
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
        error_window_past_decay_slots=error_window_past_decay_slots,
        max_error_threshold=max_error_threshold,
        prehistory_error_ratio=prehistory_error_ratio,
        carbon_random_noise_amplitude=carbon_random_noise_amplitude,
        prehistory_mock_influence=prehistory_mock_influence,
        include_prehistory=bool(include_prehistory),
    )
    save_json(output_path, scenario)
    return scenario


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser. All defaults come directly from config.py."""
    parser = argparse.ArgumentParser(description="Generate deterministic N-shift benchmark scenario.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for generated scenario JSON. Defaults to scenario_seed_<SEED>.json.",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--total-slots", type=int, default=int(config.TOTAL_SLOTS))
    parser.add_argument("--slot-duration-seconds", type=float, default=float(config.SLOT_DURATION_SECONDS))
    parser.add_argument("--requests-per-slot", type=float, default=float(config.PREDICTED_REQUESTS_PER_SLOT))
    parser.add_argument("--request-rate-std-factor", type=float, default=float(config.REQUEST_RATE_STD_FACTOR))
    parser.add_argument("--deadline-min-slack", type=int, default=0)
    parser.add_argument("--deadline-max-slack", type=int, default=int(config.ASSIGNMENT_MAX_FUTURE_SLOTS))
    parser.add_argument("--error-window-past", type=int, default=int(config.ERROR_WINDOW_PAST))
    parser.add_argument("--error-window-future", type=int, default=int(config.ERROR_WINDOW_FUTURE))
    parser.add_argument(
        "--error-window-past-decay-slots",
        type=int,
        default=int(config.ERROR_WINDOW_PAST_DECAY_SLOTS),
    )
    parser.add_argument("--max-error-threshold", type=float, default=float(config.MAX_ERROR_THRESHOLD))
    parser.add_argument(
        "--prehistory-error-ratio",
        type=float,
        default=float(config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD),
    )
    parser.add_argument(
        "--carbon-random-noise-amplitude",
        type=float,
        default=float(config.CARBON_RANDOM_NOISE_AMPLITUDE),
        help="Uniform random amplitude added to carbon intensity (+/- value).",
    )
    parser.add_argument(
        "--include-prehistory",
        action=argparse.BooleanOptionalAction,
        default=bool(config.PREHISTORY_USE_VIRTUAL_PAST),
        help="Include synthetic prehistory slots (use --no-include-prehistory to disable).",
    )
    parser.add_argument(
        "--prehistory-mock-influence",
        type=float,
        default=float(config.PREHISTORY_MOCK_INFLUENCE),
        help="Scale factor [0..1] applied to synthetic prehistory request counts.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

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
    print(f"  Error Window Past Decay Slots:     {args.error_window_past_decay_slots}")
    print(f"  Max Error Threshold (%):           {args.max_error_threshold}")
    print(f"  Prehistory Error Ratio:            {args.prehistory_error_ratio}")
    print(f"  Include Prehistory:                {args.include_prehistory}")
    print(f"  Carbon Random Noise Amplitude:     {args.carbon_random_noise_amplitude}")
    print(f"  Prehistory Mock Influence:         {args.prehistory_mock_influence}")
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
        error_window_past_decay_slots=args.error_window_past_decay_slots,
        max_error_threshold=args.max_error_threshold,
        prehistory_error_ratio=args.prehistory_error_ratio,
        carbon_random_noise_amplitude=args.carbon_random_noise_amplitude,
        prehistory_mock_influence=args.prehistory_mock_influence,
        include_prehistory=args.include_prehistory,
    )

    print(f"\nScenario saved to {args.output}")
    print(f"  Total Requests:                    {len(scenario['requests'])}")
    print(f"  Total Slots:                       {scenario['metadata']['total_slots']}")
    print(f"  Avg Requests/Slot:                 {len(scenario['requests']) / scenario['metadata']['total_slots']:.1f}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
