#!/usr/bin/env python3
"""
Example: Using Carbonshift Online Schedulers

Demonstrates all three scheduling approaches:
1. GreedyCarbonLookahead (fast heuristic)
2. ProbabilisticSlack (deadline-aware heuristic)
3. RollingWindowILP (near-optimal ILP)

Compares emissions across approaches.
"""

import sys
import os
import random

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from online.request_predictor import MockRequestPredictor
from online.heuristics import (
    GreedyCarbonLookahead,
    ProbabilisticSlackScheduler,
    Request,
    Strategy
)
from online.rolling_window_ilp import RollingWindowILPScheduler


def generate_test_requests(n=50, seed=42):
    """Generate synthetic request workload"""
    random.seed(seed)
    requests = []

    for i in range(n):
        arrival_time = i % 24  # Hourly arrivals
        deadline = arrival_time + random.randint(2, 6)  # 2-6 hours slack

        requests.append(Request(
            id=i,
            arrival_time=arrival_time,
            deadline=min(deadline, 23)  # Cap at 24h
        ))

    return requests


def calculate_emissions(assignments, strategies, carbon):
    """Calculate total emissions for assignments"""
    total_emissions = 0.0
    total_error = 0.0

    for req_id, (slot, strategy_name) in assignments.items():
        # Find strategy
        strategy = next(s for s in strategies if s.name == strategy_name)

        # Match carbonshift.py emission calculation
        # carbon[gCO2/kWh] * duration[s] / 3600 * 0.05kW = gCO2
        emission = carbon[slot] * strategy.duration / 3600 * 0.05
        total_emissions += emission
        total_error += strategy.error

    avg_error = total_error / len(assignments) if assignments else 0.0

    return total_emissions, avg_error


def main():
    print("=" * 70)
    print("Carbonshift Online Scheduling - Example Comparison")
    print("=" * 70)

    # Setup: Strategies
    strategies = [
        Strategy(name="High", error=0, duration=120),
        Strategy(name="Medium", error=2, duration=60),
        Strategy(name="Low", error=5, duration=30),
    ]

    # Setup: Carbon intensity forecast (24-hour pattern)
    carbon = [
        120, 110, 100, 95, 90, 100,   # Night (low, 0-5 AM)
        110, 130, 160, 180, 200, 210, # Morning (rising, 6-11 AM)
        200, 190, 180, 170, 180, 200, # Afternoon (12-17 PM)
        220, 210, 190, 170, 150, 130  # Evening (falling, 18-23 PM)
    ]

    print(f"\nCarbon Intensity Pattern (gCO2/kWh):")
    print(f"  Greenest slot: {carbon.index(min(carbon))} ({min(carbon)} gCO2/kWh)")
    print(f"  Dirtiest slot: {carbon.index(max(carbon))} ({max(carbon)} gCO2/kWh)")
    print(f"  Average: {sum(carbon)/len(carbon):.1f} gCO2/kWh")

    # Setup: Request predictor
    predictor = MockRequestPredictor(
        base_load=50.0,
        morning_peak_hour=9,
        evening_peak_hour=19,
        seed=42
    )

    # Generate test workload
    print(f"\nGenerating 50 test requests...")
    requests = generate_test_requests(n=50, seed=42)

    print(f"  Requests generated: {len(requests)}")
    print(f"  Deadline range: {min(r.deadline for r in requests)} - {max(r.deadline for r in requests)}")

    # ========================================================================
    # APPROACH 1: Baseline (immediate, High quality)
    # ========================================================================
    print("\n" + "-" * 70)
    print("APPROACH 1: Baseline (immediate scheduling, High quality)")
    print("-" * 70)

    baseline_assignments = {}
    for req in requests:
        # Naive: always schedule immediately with High quality
        baseline_assignments[req.id] = (req.arrival_time, "High")

    baseline_emissions, baseline_error = calculate_emissions(
        baseline_assignments, strategies, carbon
    )

    print(f"Total emissions: {baseline_emissions:,.0f}")
    print(f"Average error: {baseline_error:.2f}%")

    # ========================================================================
    # APPROACH 2: GreedyCarbonLookahead
    # ========================================================================
    print("\n" + "-" * 70)
    print("APPROACH 2: GreedyCarbonLookahead (carbon-aware + pressure)")
    print("-" * 70)

    lookahead = GreedyCarbonLookahead(
        strategies=strategies,
        carbon=carbon,
        capacity=10,  # Low capacity to test pressure
        pressure_weight=0.5,
        error_threshold=5.0,
        predictor=predictor
    )

    lookahead_assignments = {}
    for req in requests:
        slot, strategy = lookahead.schedule(req, current_time=req.arrival_time)
        lookahead_assignments[req.id] = (slot, strategy)

    lookahead_emissions, lookahead_error = calculate_emissions(
        lookahead_assignments, strategies, carbon
    )

    print(f"Total emissions: {lookahead_emissions:,.0f} ({(lookahead_emissions/baseline_emissions - 1)*100:+.1f}%)")
    print(f"Average error: {lookahead_error:.2f}%")

    # Show some assignments
    print("\nSample assignments:")
    for i in range(min(5, len(requests))):
        req = requests[i]
        slot, strategy = lookahead_assignments[req.id]
        print(f"  Request {req.id} (arrive={req.arrival_time}, deadline={req.deadline}) → slot {slot}, {strategy}")

    # ========================================================================
    # APPROACH 3: ProbabilisticSlack
    # ========================================================================
    print("\n" + "-" * 70)
    print("APPROACH 3: ProbabilisticSlack (deadline-aware)")
    print("-" * 70)

    slack_scheduler = ProbabilisticSlackScheduler(
        strategies=strategies,
        carbon=carbon,
        capacity=10,
        slack_threshold=3,
        error_threshold=5.0,
        predictor=predictor
    )

    slack_assignments = {}
    for req in requests:
        slot, strategy = slack_scheduler.schedule(req, current_time=req.arrival_time)
        slack_assignments[req.id] = (slot, strategy)

    slack_emissions, slack_error = calculate_emissions(
        slack_assignments, strategies, carbon
    )

    print(f"Total emissions: {slack_emissions:,.0f} ({(slack_emissions/baseline_emissions - 1)*100:+.1f}%)")
    print(f"Average error: {slack_error:.2f}%")

    # ========================================================================
    # APPROACH 4: Rolling Window ILP (optional - requires carbonshift.py)
    # ========================================================================
    print("\n" + "-" * 70)
    print("APPROACH 4: Rolling Window ILP (near-optimal)")
    print("-" * 70)

    # Check if carbonshift.py exists
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    carbonshift_path = os.path.join(project_root, 'carbonshift.py')

    if os.path.exists(carbonshift_path):
        print("Found carbonshift.py - running ILP optimization...")

        ilp_scheduler = RollingWindowILPScheduler(
            strategies=strategies,
            carbon=carbon,
            window_size=5,
            reopt_interval=0,  # Trigger every time
            ilp_timeout=10.0,
            error_threshold=5,
            predictor=predictor,
            carbonshift_path=carbonshift_path
        )

        ilp_assignments = {}
        for req in requests:
            slot, strategy = ilp_scheduler.schedule_request(req, current_time=req.arrival_time)
            ilp_assignments[req.id] = (slot, strategy)

        ilp_emissions, ilp_error = calculate_emissions(
            ilp_assignments, strategies, carbon
        )

        print(f"Total emissions: {ilp_emissions:,.0f} ({(ilp_emissions/baseline_emissions - 1)*100:+.1f}%)")
        print(f"Average error: {ilp_error:.2f}%")
    else:
        print("carbonshift.py not found - skipping ILP approach")
        print("(Place carbonshift.py in parent directory to enable)")
        ilp_emissions = None

    # ========================================================================
    # SUMMARY
    # ========================================================================
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    results = [
        ("Baseline (immediate, High)", baseline_emissions, baseline_error),
        ("GreedyCarbonLookahead", lookahead_emissions, lookahead_error),
        ("ProbabilisticSlack", slack_emissions, slack_error),
    ]

    if ilp_emissions is not None:
        results.append(("Rolling Window ILP", ilp_emissions, ilp_error))

    print(f"\n{'Approach':<30} {'Emissions':>15} {'vs Baseline':>12} {'Avg Error':>10}")
    print("-" * 70)

    for name, emissions, error in results:
        vs_baseline = (emissions / baseline_emissions - 1) * 100
        print(f"{name:<30} {emissions:>15,.0f} {vs_baseline:>11.1f}% {error:>10.2f}%")

    # Find best
    best_name, best_emissions, best_error = min(results, key=lambda x: x[1])
    print("\n" + "=" * 70)
    print(f"✓ Best approach: {best_name}")
    print(f"  Emissions: {best_emissions:,.0f} ({(best_emissions/baseline_emissions - 1)*100:+.1f}% vs baseline)")
    print(f"  Average error: {best_error:.2f}%")
    print("=" * 70)

    # Visualize slot distribution
    print("\nSlot Load Distribution (GreedyCarbonLookahead):")
    print("-" * 70)

    slot_loads = [0] * 24
    for req_id, (slot, strategy) in lookahead_assignments.items():
        slot_loads[slot] += 1

    max_load = max(slot_loads)
    for slot, load in enumerate(slot_loads):
        bar_len = int((load / max_load) * 30) if max_load > 0 else 0
        bar = "█" * bar_len
        carbon_val = carbon[slot]
        print(f"Slot {slot:2d} ({carbon_val:3d} gCO2): {load:3d} reqs {bar}")

    print("\nNote: Scheduler should avoid high-carbon slots (e.g., slot 18: 220 gCO2)")
    print("=" * 70)


if __name__ == '__main__':
    main()
