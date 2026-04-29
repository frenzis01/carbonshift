#!/usr/bin/env python3
"""
Comparison of Online Schedulers

Compares:
1. Heuristic (Greedy Carbon Lookahead)
2. Rolling Window ILP
3. Rolling Window DP (no pruning baseline - if fast enough)
4. Rolling Window DP + K-Best Pruning
5. Rolling Window DP + Beam Search

Configuration:
- Request predictor: DISABLED (realistic online scenario)
- Requests arrive in batches at each time slot
- Carbon intensity follows realistic daily pattern
"""

import sys
import os
import time
import random

# Add parent directories to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rolling_window_ilp import RollingWindowILPScheduler, Request, Strategy
from rolling_window_dp import RollingWindowDPScheduler
from heuristics import GreedyCarbonLookahead


def print_header(title):
    print("\n" + "=" * 80)
    print(title.center(80))
    print("=" * 80)


def generate_test_scenario(num_slots=20, requests_per_slot=3, seed=42):
    """
    Generate test scenario for comparison.

    Args:
        num_slots: Total time slots
        requests_per_slot: Average requests arriving per slot
        seed: Random seed for reproducibility

    Returns:
        (requests_by_slot, strategies, carbon)
        
    Note:
        Carbon intensity follows a 24-hour periodic pattern:
        - Low at midnight (hour 0, 24): ~400 gCO2/kWh
        - Peak at midday (hour 12): ~1200 gCO2/kWh (3x difference!)
        - Pattern repeats every 24 slots
        - Large variation creates strong optimization incentive
    """
    random.seed(seed)

    # Carbon intensity: EXTREME daily pattern to force interesting choices
    # Periodic pattern with 24-slot day cycle
    import math
    carbon = []
    day_cycle = 24  # Slots in a full day cycle
    
    for slot in range(num_slots):
        # Sinusoidal pattern repeating every 24 slots
        # Peak at hour 12 (midday), low at hour 0 and 24 (midnight)
        phase = (slot % day_cycle) / day_cycle  # 0 to 1 over one cycle
        
        # Extreme range: 3x difference between night and day!
        base = 400  # gCO2/kWh baseline (nighttime minimum)
        amplitude = 800  # Peak goes to 1200 gCO2/kWh
        
        # Use cosine shifted so midnight (phase=0) is minimum
        value = base + amplitude * (1 - math.cos(2 * math.pi * phase)) / 2
        
        # Add small random variation (±5%) to make it more realistic
        noise = random.uniform(-0.05, 0.05) * value
        carbon.append(value + noise)

    # Strategies: EXTREME error/duration tradeoffs
    # Key: Fast strategy saves carbon (shorter) but costs accuracy
    # Accurate strategy costs carbon (longer) but provides accuracy
    # With tight error budget, must balance carefully!
    strategies = [
        Strategy(name='Accurate', error=1, duration=300),   # 5 min - very accurate but SLOW (high carbon cost)
        Strategy(name='Balanced', error=2.5, duration=120), # 2 min - middle ground
        Strategy(name='Fast', error=5, duration=30),        # 30 sec - fast (low carbon) but less accurate
    ]

    # Generate requests with BURST pattern and SHORT deadlines
    requests_by_slot = {}
    req_id = 0

    for slot in range(num_slots):
        # BURST pattern: more requests during certain hours
        hour = slot % 24
        if 8 <= hour <= 18:  # Business hours: MORE requests
            base_reqs = requests_per_slot * 2
        else:  # Off hours: FEWER requests
            base_reqs = requests_per_slot // 2 + 1
        
        # Poisson-like arrival with bursts
        num_requests = max(1, int(random.gauss(base_reqs, base_reqs * 0.3)))

        slot_requests = []
        for _ in range(num_requests):
            # Deadlines: MIX of VERY tight (forces bad slots) and loose (allows optimization)
            max_slack = num_slots - slot - 1
            
            if max_slack < 2:
                # Very close to end, use whatever is available
                deadline = num_slots - 1
            elif random.random() < 0.6:  # 60% TIGHT deadlines!
                # Tight deadline: 2-4 slots only! Forces suboptimal carbon choices
                slack = random.randint(2, min(4, max_slack))
                deadline = slot + slack
            elif max_slack >= 5:
                # Loose deadline: allows optimization
                slack = random.randint(5, min(15, max_slack))
                deadline = slot + slack
            else:
                # Not enough space for loose, use tight
                slack = random.randint(2, max_slack)
                deadline = slot + slack

            slot_requests.append(
                Request(
                    id=req_id,
                    deadline=deadline,
                    arrival_time=slot
                )
            )
            req_id += 1

        requests_by_slot[slot] = slot_requests

    return requests_by_slot, strategies, carbon


def run_scheduler(
    scheduler,
    requests_by_slot,
    name="Scheduler"
):
    """
    Run a scheduler on the test scenario.

    Returns:
        (total_cost, total_error, assignments, runtime)
    """
    start_time = time.time()

    assignments = {}  # {req_id: (slot, strategy)}
    total_cost = 0.0
    total_error = 0
    total_requests = 0

    # Process each time slot
    for current_slot in sorted(requests_by_slot.keys()):
        # Process arrivals for this slot
        for request in requests_by_slot[current_slot]:
            slot, strategy_name = scheduler.schedule_request(request, current_slot)
            assignments[request.id] = (slot, strategy_name)

        # Commit the slot
        if hasattr(scheduler, 'commit_slot'):
            scheduler.commit_slot(current_slot)

    runtime = time.time() - start_time

    # Calculate cost and error from assignments
    # (Note: This is approximate since we don't track actual carbon and strategy details
    #  in the scheduler's return values. In practice, you'd query the scheduler state.)

    return total_cost, total_error, assignments, runtime


def evaluate_assignments(
    assignments,
    strategies,
    carbon,
    strategy_map
):
    """
    Evaluate quality of assignments.

    Args:
        assignments: {req_id: (slot, strategy_name)}
        strategies: List of available strategies
        carbon: Carbon intensity per slot
        strategy_map: {strategy_name: Strategy}

    Returns:
        (total_cost, avg_error, total_requests)
    """
    total_cost = 0.0
    total_error = 0
    total_requests = len(assignments)

    for req_id, (slot, strategy_name) in assignments.items():
        strategy = strategy_map[strategy_name]

        # Cost: carbon intensity at slot (simplified: 1 request = 1 unit)
        cost = carbon[slot]
        total_cost += cost

        # Error accumulation
        total_error += strategy.error

    avg_error = total_error / total_requests if total_requests > 0 else 0

    return total_cost, avg_error, total_requests


def run_comparison():
    """Run comprehensive scheduler comparison"""

    print_header("ONLINE SCHEDULER COMPARISON")

    # Configuration
    print("\n📋 Configuration:")
    print("  - Request Predictor: DISABLED (realistic online scenario)")
    print("  - Scenario: 48 time slots (2 days), bursty arrivals")
    print("  - Error Threshold: 3% (VERY tight, forces Accurate strategy usage)")
    print("  - Carbon Range: 3x difference (400-1200 gCO2/kWh)")
    print("  - Deadlines: 60% tight (2-4 slots), creates optimization pressure")
    print("  - Strategies: 3 (Accurate/Balanced/Fast)")
    print("  - Carbon: Realistic daily pattern")

    # Generate scenario
    num_slots = 48  # 2 full day cycles
    requests_per_slot = 3  # Base rate (will vary with burst pattern)

    print(f"\n🔧 Generating test scenario...")
    requests_by_slot, strategies, carbon = generate_test_scenario(
        num_slots=num_slots,
        requests_per_slot=requests_per_slot
    )

    total_requests = sum(len(reqs) for reqs in requests_by_slot.values())
    print(f"  Total requests: {total_requests}")
    print(f"  Carbon range: [{min(carbon):.0f}, {max(carbon):.0f}] gCO2/kWh")

    # Strategy map for evaluation
    strategy_map = {s.name: s for s in strategies}

    error_threshold = 3.0  # 3% average error limit (VERY tight constraint!)

    results = []

    # 1. Heuristic Baseline
    print_header("1. HEURISTIC (Greedy Carbon Lookahead)")
    heuristic = GreedyCarbonLookahead(
        strategies=strategies,
        carbon=carbon,
        predictor=None,  # No predictor
        error_threshold=error_threshold
    )

    start = time.time()
    assignments_h = {}
    for current_slot in sorted(requests_by_slot.keys()):
        for request in requests_by_slot[current_slot]:
            slot, strategy_name = heuristic.schedule(request, current_slot)
            assignments_h[request.id] = (slot, strategy_name)
    time_h = time.time() - start

    cost_h, error_h, _ = evaluate_assignments(
        assignments_h, strategies, carbon, strategy_map
    )

    print(f"  ✅ Cost: {cost_h:.2f}, Error: {error_h:.2f}%, Time: {time_h:.4f}s")
    results.append(('Heuristic', cost_h, error_h, time_h, None))

    # 2. Rolling Window ILP
    print_header("2. ROLLING WINDOW ILP")
    try:
        ilp_scheduler = RollingWindowILPScheduler(
            strategies=strategies,
            carbon=carbon,
            window_size=5,
            reopt_interval=2,  # Re-optimize every 2 time slots
            ilp_timeout=5.0,
            error_threshold=error_threshold,
            predictor=None  # No predictor
        )

        start = time.time()
        assignments_ilp = {}
        for current_slot in sorted(requests_by_slot.keys()):
            for request in requests_by_slot[current_slot]:
                slot, strategy_name = ilp_scheduler.schedule_request(request, current_slot)
                assignments_ilp[request.id] = (slot, strategy_name)
            ilp_scheduler.commit_slot(current_slot)
        time_ilp = time.time() - start

        cost_ilp, error_ilp, _ = evaluate_assignments(
            assignments_ilp, strategies, carbon, strategy_map
        )

        print(f"  ✅ Cost: {cost_ilp:.2f}, Error: {error_ilp:.2f}%, Time: {time_ilp:.4f}s")
        results.append(('RW-ILP', cost_ilp, error_ilp, time_ilp, None))

    except Exception as e:
        print(f"  ❌ Failed: {e}")
        results.append(('RW-ILP', None, None, None, None))

    # 3. Rolling Window DP + K-Best
    print_header("3. ROLLING WINDOW DP + K-BEST PRUNING")
    dp_kb_scheduler = RollingWindowDPScheduler(
        strategies=strategies,
        carbon=carbon,
        window_size=5,
        reopt_interval=2,
        dp_timeout=5.0,
        error_threshold=error_threshold,
        predictor=None,
        pruning='kbest',
        pruning_k=150
    )

    start = time.time()
    assignments_dp_kb = {}
    for current_slot in sorted(requests_by_slot.keys()):
        for request in requests_by_slot[current_slot]:
            slot, strategy_name = dp_kb_scheduler.schedule_request(request, current_slot)
            assignments_dp_kb[request.id] = (slot, strategy_name)
        dp_kb_scheduler.commit_slot(current_slot)
    time_dp_kb = time.time() - start

    cost_dp_kb, error_dp_kb, _ = evaluate_assignments(
        assignments_dp_kb, strategies, carbon, strategy_map
    )

    print(f"  ✅ Cost: {cost_dp_kb:.2f}, Error: {error_dp_kb:.2f}%, Time: {time_dp_kb:.4f}s")
    results.append(('RW-DP+KBest', cost_dp_kb, error_dp_kb, time_dp_kb, None))

    # 4. Rolling Window DP + Beam Search
    print_header("4. ROLLING WINDOW DP + BEAM SEARCH")
    dp_beam_scheduler = RollingWindowDPScheduler(
        strategies=strategies,
        carbon=carbon,
        window_size=5,
        reopt_interval=2,
        dp_timeout=5.0,
        error_threshold=error_threshold,
        predictor=None,
        pruning='beam',
        pruning_k=150
    )

    start = time.time()
    assignments_dp_beam = {}
    for current_slot in sorted(requests_by_slot.keys()):
        for request in requests_by_slot[current_slot]:
            slot, strategy_name = dp_beam_scheduler.schedule_request(request, current_slot)
            assignments_dp_beam[request.id] = (slot, strategy_name)
        dp_beam_scheduler.commit_slot(current_slot)
    time_dp_beam = time.time() - start

    cost_dp_beam, error_dp_beam, _ = evaluate_assignments(
        assignments_dp_beam, strategies, carbon, strategy_map
    )

    print(f"  ✅ Cost: {cost_dp_beam:.2f}, Error: {error_dp_beam:.2f}%, Time: {time_dp_beam:.4f}s")
    results.append(('RW-DP+Beam', cost_dp_beam, error_dp_beam, time_dp_beam, None))

    # Summary
    print_header("COMPARISON SUMMARY")

    # Find best cost
    valid_costs = [r[1] for r in results if r[1] is not None]
    best_cost = min(valid_costs) if valid_costs else None

    print(f"\n{'Method':<20} {'Cost':>12} {'Error':>8} {'Gap':>8} {'Time':>10}")
    print("-" * 70)

    for name, cost, error, runtime, _ in results:
        if cost is None:
            print(f"{name:<20} {'FAILED':>12} {'-':>8} {'-':>8} {'-':>10}")
        else:
            gap = ((cost - best_cost) / best_cost * 100) if best_cost else 0
            is_best = (cost == best_cost)
            marker = "🏆" if is_best else "  "
            print(f"{marker}{name:<18} {cost:>12,.2f} {error:>7.2f}% {gap:>7.1f}% "
                  f"{runtime:>9.4f}s")

    print("\n🏆 = Optimal solution (lowest cost)")

    # Key insights
    print_header("KEY INSIGHTS")

    if cost_h and best_cost:
        heuristic_gap = (cost_h - best_cost) / best_cost * 100
        print(f"\n📊 Heuristic vs Optimal:")
        print(f"   Gap: {heuristic_gap:+.1f}%")

        if cost_dp_kb and cost_dp_beam:
            best_dp = min(cost_dp_kb, cost_dp_beam)
            dp_improvement = (cost_h - best_dp) / cost_h * 100
            print(f"\n✅ DP Improvement over Heuristic:")
            print(f"   {dp_improvement:.1f}%")

            if time_dp_kb and time_h:
                speedup_kb = time_h / time_dp_kb
                print(f"\n⚡ Performance:")
                print(f"   K-Best: {speedup_kb:.1f}× relative to heuristic")
                if time_dp_beam:
                    speedup_beam = time_h / time_dp_beam
                    print(f"   Beam: {speedup_beam:.1f}× relative to heuristic")


if __name__ == '__main__':
    run_comparison()
