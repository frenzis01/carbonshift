#!/usr/bin/env python3
"""Test if seed is the issue"""

import sys
import os
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probabilistic_slack_cont import probabilistic_slack_contiguous

# Use same parameters as comparison.py
num_blocks = 10
delta = 8
error_threshold = 5
slot_duration_minutes = 30
parallelism = 3

# Generate test instance - this uses random
random.seed(42)

blocks = []
for i in range(num_blocks):
    size = random.randint(2, 6)
    deadline = min(delta - 1, random.randint(3, 7))
    blocks.append({'size': size, 'deadline': deadline})

# Now random state is different!
print(f"Random state after instance generation: {random.getstate()[1][:5]}")

strategies = [
    {'name': 'Low-Error-Long', 'error': 1, 'duration': 35.0},
    {'name': 'Mid-Error-Mid', 'error': 3, 'duration': 18.0},
    {'name': 'High-Error-Short', 'error': 6, 'duration': 8.0},
]

carbon = [100, 95, 90, 80, 70, 60, 50, 40]
capacity_tiers = []

print("\n" + "=" * 80)
print("Test 1: Call without resetting seed (like in comparison)")
print("=" * 80)

# Don't reset seed - probabilistic_slack will use seed=42 internally
cost1, error1, assign1, resid1 = probabilistic_slack_contiguous(
    blocks, strategies, carbon, delta, error_threshold,
    slot_duration_minutes, parallelism, capacity_tiers
)

print(f"Result: {'SUCCESS' if cost1 else 'FAILED'}")
if cost1:
    print(f"  Cost: {cost1:.2f}, Error: {error1:.2f}%")

print("\n" + "=" * 80)
print("Test 2: Call with explicit seed reset")
print("=" * 80)

# Reset to a fresh state
cost2, error2, assign2, resid2 = probabilistic_slack_contiguous(
    blocks, strategies, carbon, delta, error_threshold,
    slot_duration_minutes, parallelism, capacity_tiers,
    seed=123  # Different seed
)

print(f"Result: {'SUCCESS' if cost2 else 'FAILED'}")
if cost2:
    print(f"  Cost: {cost2:.2f}, Error: {error2:.2f}%")
