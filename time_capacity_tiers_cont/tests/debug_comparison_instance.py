#!/usr/bin/env python3
"""Debug the test instance used in comparison"""

import sys
import os
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probabilistic_slack_cont import probabilistic_slack_contiguous

# Use same parameters as comparison.py
num_blocks = 10
delta = 8
error_threshold = 5  # 5%
slot_duration_minutes = 30
parallelism = 3

# Generate test instance with SAME seed
random.seed(42)

blocks = []
for i in range(num_blocks):
    size = random.randint(2, 6)
    deadline = min(delta - 1, random.randint(3, 7))
    blocks.append({'size': size, 'deadline': deadline})

strategies = [
    {'name': 'Low-Error-Long', 'error': 1, 'duration': 35.0},
    {'name': 'Mid-Error-Mid', 'error': 3, 'duration': 18.0},
    {'name': 'High-Error-Short', 'error': 6, 'duration': 8.0},
]

carbon = [100, 95, 90, 80, 70, 60, 50, 40]
capacity_tiers = []

print("Test Instance:")
print(f"  Blocks: {num_blocks}")
print(f"  Total Requests: {sum(b['size'] for b in blocks)}")
print(f"  Blocks detail:")
for i, b in enumerate(blocks):
    print(f"    Block {i}: size={b['size']}, deadline={b['deadline']}")
print(f"\n  Strategies:")
for s in strategies:
    print(f"    {s['name']}: error={s['error']}%, duration={s['duration']} min")
print(f"\n  Total capacity: {delta} slots × {slot_duration_minutes * parallelism} min = {delta * slot_duration_minutes * parallelism} min")
print(f"  Total work needed (worst case): {sum(b['size'] * max(s['duration'] for s in strategies) for b in blocks)} min")

print("\n" + "=" * 80)
print("Testing Probabilistic Slack...")
print("=" * 80)

# Reset random seed for scheduler
cost, error, assign, resid = probabilistic_slack_contiguous(
    blocks, strategies, carbon, delta, error_threshold,
    slot_duration_minutes, parallelism, capacity_tiers,
    slack_threshold=3, seed=42
)

if cost is None:
    print("❌ FAILED")
else:
    print(f"✅ SUCCESS")
    print(f"Cost: {cost:.2f}")
    print(f"Error: {error:.2f}%")
    print(f"Assignments: {len(assign)}")
