"""Default configuration values for benchmark scenario generation.

Single source of truth for every parameter used by `generate_scenario.py`
and by `tests/battery/run_battery.py`'s `_generate_scenario()` helper.

These defaults are intentionally **decoupled** from `online2/config.py`
(the live scheduler's runtime configuration): a benchmark scenario describes
a fixed input workload, and it should not silently change just because the
scheduler's own tunables (DP pruning, solver strategy, logging paths, …) are
edited. Any of these values can be overridden per-scenario in
`battery_config.json` or via `generate_scenario.py` CLI flags without
touching the scheduler config at all.
"""

from __future__ import annotations

from typing import Any, Dict, List

# ── time slot geometry ───────────────────────────────────────────────────────
TOTAL_SLOTS: int = 24
SLOT_DURATION_SECONDS: float = 10.0

# ── request generation ───────────────────────────────────────────────────────
# Target average requests per slot (see REQUEST_NIGHT_FLOOR_RATIO below for
# how this average is distributed across the day/night cycle).
PREDICTED_REQUESTS_PER_SLOT: float = 60.0
# Gaussian noise factor: sigma = max(1, mean_rate_for_slot * REQUEST_RATE_STD_FACTOR).
# See the inline note in scenario_io.generate_scenario_data for tuning guidance.
REQUEST_RATE_STD_FACTOR: float = 0.25
DEADLINE_MIN_SLACK: int = 0
DEADLINE_MAX_SLACK: int = 8

# ── error budget / prehistory ────────────────────────────────────────────────
MAX_ERROR_THRESHOLD: float = 4.0
ERROR_WINDOW_PAST: int = 12
ERROR_WINDOW_FUTURE: int = 8
ERROR_WINDOW_PAST_DECAY_SLOTS: int = 0
PREHISTORY_USE_VIRTUAL_PAST: bool = False
PREHISTORY_ERROR_RATIO_OF_THRESHOLD: float = 1.0
PREHISTORY_MOCK_INFLUENCE: float = 0.4

# ── carbon intensity daylight wave ───────────────────────────────────────────
# Period (slots) of the sinusoidal carbon-intensity wave. 24 == one daily cycle
# when each slot represents one hour.
CARBON_INTENSITY_CYCLE_SLOTS: int = 24
# NOTE: this value is recorded in scenario metadata for backward-compat but is
# NOT currently consumed by generate_carbon_intensity_forecast (which instead
# uses CARBON_NOISE_STD / CARBON_NOISE_PERSISTENCE below for an AR(1) noise
# process). Kept only so existing scenario JSON files / readers relying on the
# field keep working.
CARBON_RANDOM_NOISE_AMPLITUDE: float = 20.0
CARBON_NIGHT_MAX: float = 75.0
CARBON_DAY_MIN: float = 170.0
CARBON_SUNRISE_FRACTION: float = 0.30
CARBON_SUNSET_FRACTION: float = 0.78
CARBON_TRANSITION_SLOPE: float = 20.0
CARBON_NOISE_STD: float = 1.5
CARBON_NOISE_PERSISTENCE: float = 0.97

# ── request-rate daylight wave ───────────────────────────────────────────────
# Requests follow the same sunrise/sunset/transition_slope shape as carbon
# intensity (see CARBON_* above). REQUEST_NIGHT_FLOOR_RATIO controls how low
# the night valley dips relative to the daytime peak, before the whole curve
# is rescaled so its average equals PREDICTED_REQUESTS_PER_SLOT.
REQUEST_NIGHT_FLOOR_RATIO: float = 0.40

# ── capacity tiers (rebound effect) ──────────────────────────────────────────
# Step-function carbon multiplier based on slot request count. Overridable
# per-scenario via the "capacity_tiers" key in battery_config.json.
CAPACITY_TIERS: List[Dict[str, Any]] = [
    {"max_requests": 30, "multiplier": 1.0},
    {"max_requests": 50, "multiplier": 1.5},
    {"max_requests": 80, "multiplier": 2.0},
    {"max_requests": None, "multiplier": 5.0},
]
