"""Generate deterministic scenario JSON for N-shift speed benchmarks."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Dict, Optional

# ─── path setup ──────────────────────────────────────────────────────────────
# This file lives at tests/battery/scenarios/generate_scenario.py.
# All scenario-generation config now lives locally in scenario_config.py, so
# no dependency on online2/config.py (the live scheduler's runtime config) is
# needed here — the two are intentionally decoupled.
SCENARIOS_DIR = Path(__file__).resolve().parent
SCENARIOS_JSON_DIR = SCENARIOS_DIR / "json"

if str(SCENARIOS_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIOS_DIR))

import scenario_config as cfg
from scenario_io import generate_scenario_data, save_json


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
    carbon_intensity_cycle_slots: Optional[int] = None,
    capacity_tiers: Optional[list] = None,
) -> Dict[str, Any]:
    if prehistory_mock_influence is None:
        prehistory_mock_influence = float(cfg.PREHISTORY_MOCK_INFLUENCE)
    if error_window_past_decay_slots is None:
        error_window_past_decay_slots = int(cfg.ERROR_WINDOW_PAST_DECAY_SLOTS)
    if include_prehistory is None:
        include_prehistory = bool(cfg.PREHISTORY_USE_VIRTUAL_PAST)
    if carbon_intensity_cycle_slots is None:
        carbon_intensity_cycle_slots = int(cfg.CARBON_INTENSITY_CYCLE_SLOTS)
    if capacity_tiers is None:
        capacity_tiers = list(cfg.CAPACITY_TIERS)
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
        carbon_intensity_cycle_slots=carbon_intensity_cycle_slots,
        capacity_tiers=capacity_tiers,
        carbon_night_max=float(cfg.CARBON_NIGHT_MAX),
        carbon_day_min=float(cfg.CARBON_DAY_MIN),
        carbon_sunrise_fraction=float(cfg.CARBON_SUNRISE_FRACTION),
        carbon_sunset_fraction=float(cfg.CARBON_SUNSET_FRACTION),
        carbon_transition_slope=float(cfg.CARBON_TRANSITION_SLOPE),
        carbon_noise_std=float(cfg.CARBON_NOISE_STD),
        carbon_noise_persistence=float(cfg.CARBON_NOISE_PERSISTENCE),
        carbon_inverted=bool(cfg.CARBON_INVERTED),
        request_night_floor_ratio=float(cfg.REQUEST_NIGHT_FLOOR_RATIO),
    )
    save_json(output_path, scenario)
    return scenario


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser. All defaults come directly from scenario_config.py."""
    parser = argparse.ArgumentParser(description="Generate deterministic N-shift benchmark scenario.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for generated scenario JSON. Defaults to tests/battery/scenarios/json/scenario_seed_<SEED>.json.",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--total-slots", type=int, default=int(cfg.TOTAL_SLOTS))
    parser.add_argument("--slot-duration-seconds", type=float, default=float(cfg.SLOT_DURATION_SECONDS))
    parser.add_argument("--requests-per-slot", type=float, default=float(cfg.PREDICTED_REQUESTS_PER_SLOT))
    parser.add_argument("--request-rate-std-factor", type=float, default=float(cfg.REQUEST_RATE_STD_FACTOR))
    parser.add_argument("--deadline-min-slack", type=int, default=int(cfg.DEADLINE_MIN_SLACK))
    parser.add_argument("--deadline-max-slack", type=int, default=int(cfg.DEADLINE_MAX_SLACK))
    parser.add_argument("--error-window-past", type=int, default=int(cfg.ERROR_WINDOW_PAST))
    parser.add_argument("--error-window-future", type=int, default=int(cfg.ERROR_WINDOW_FUTURE))
    parser.add_argument(
        "--error-window-past-decay-slots",
        type=int,
        default=int(cfg.ERROR_WINDOW_PAST_DECAY_SLOTS),
    )
    parser.add_argument("--max-error-threshold", type=float, default=float(cfg.MAX_ERROR_THRESHOLD))
    parser.add_argument(
        "--prehistory-error-ratio",
        type=float,
        default=float(cfg.PREHISTORY_ERROR_RATIO_OF_THRESHOLD),
    )
    parser.add_argument(
        "--carbon-random-noise-amplitude",
        type=float,
        default=float(cfg.CARBON_RANDOM_NOISE_AMPLITUDE),
        help="Recorded in scenario metadata only; currently unused by the forecast generator "
             "(see scenario_config.CARBON_RANDOM_NOISE_AMPLITUDE docstring).",
    )
    parser.add_argument(
        "--include-prehistory",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg.PREHISTORY_USE_VIRTUAL_PAST),
        help="Include synthetic prehistory slots (use --no-include-prehistory to disable).",
    )
    parser.add_argument(
        "--prehistory-mock-influence",
        type=float,
        default=float(cfg.PREHISTORY_MOCK_INFLUENCE),
        help="Scale factor [0..1] applied to synthetic prehistory request counts.",
    )
    parser.add_argument(
        "--carbon-intensity-cycle-slots",
        type=int,
        default=int(cfg.CARBON_INTENSITY_CYCLE_SLOTS),
        help="Period (slots) of the sinusoidal carbon-intensity wave (requests follow the same wave).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.output is None:
        SCENARIOS_JSON_DIR.mkdir(parents=True, exist_ok=True)
        args.output = SCENARIOS_JSON_DIR / f"scenario_seed_{args.seed}.json"

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
    print(f"  Carbon Intensity Cycle Slots:      {args.carbon_intensity_cycle_slots}")
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
        carbon_intensity_cycle_slots=args.carbon_intensity_cycle_slots,
    )

    print(f"\nScenario saved to {args.output}")
    print(f"  Total Requests:                    {len(scenario['requests'])}")
    print(f"  Total Slots:                       {scenario['metadata']['total_slots']}")
    print(f"  Avg Requests/Slot:                 {len(scenario['requests']) / scenario['metadata']['total_slots']:.1f}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
