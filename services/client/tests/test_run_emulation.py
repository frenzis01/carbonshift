"""Emulation's optional global capacity tiers retain the old workload ladder."""
from __future__ import annotations

from scripts.run_emulation import build_capacity_tiers


def test_capacity_tiers_scale_from_requests_per_slot():
    assert build_capacity_tiers(4) == [
        {"max_requests": 4, "multiplier": 1.0},
        {"max_requests": 6, "multiplier": 1.5},
        {"max_requests": None, "multiplier": 5.0},
    ]


def test_capacity_tiers_keep_finite_bounds_increasing_for_small_loads():
    tiers = build_capacity_tiers(1)
    assert tiers == [
        {"max_requests": 1, "multiplier": 1.0},
        {"max_requests": 2, "multiplier": 1.5},
        {"max_requests": None, "multiplier": 5.0},
    ]
    assert tiers[0]["max_requests"] < tiers[1]["max_requests"]
