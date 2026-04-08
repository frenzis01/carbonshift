#!/usr/bin/env python3
"""
Clean comparison of all schedulers for time-capacity contiguous model.
Includes heuristics and DP with pruning.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from greedy_cont import greedy_contiguous
from first_fit_cont import first_fit_contiguous
from best_fit_cont import best_fit_contiguous
from probabilistic_slack_cont import probabilistic_slack_contiguous
from dp_cont import dp_contiguous
from utils_cont import validate_solution


def print_header(title):
    print("\n" + "=" * 80)
    print(title.center(80))
    print("=" * 80)


def print_assignment_distribution(assignments, carbon, delta):
    """Show how requests are distributed across slots"""
    slot_counts = [0] * delta
    slot_times = [0.0] * delta
    
    for block_idx, strategy_idx, start_slot, slots_used in assignments:
        for slot_idx, time_used, load in slots_used:
            slot_counts[slot_idx] += 1
            slot_times[slot_idx] += time_used
    
    print("\n  Slot Distribution:")
    for slot_idx in range(delta):
        if slot_counts[slot_idx] > 0 or slot_times[slot_idx] > 0:
            print(f"    Slot {slot_idx}: {slot_counts[slot_idx]:2d} requests, "
                  f"{slot_times[slot_idx]:6.1f} min, carbon={carbon[slot_idx]}")


def run_comparison():
    """Run comparison of all schedulers"""
    
    print_header("CONTIGUOUS TIME-CAPACITY SCHEDULERS COMPARISON")
    
    # Test parameters
    num_blocks = 12
    delta = 8
    error_threshold = 5  # 5%
    slot_duration_minutes = 30
    parallelism = 3
    
    # Generate test instance
    import random
    random.seed(42)
    
    blocks = []
    for i in range(num_blocks):
        size = random.randint(2, 12)
        deadline = min(delta - 1, random.randint(3, 7))
        blocks.append({'size': size, 'deadline': deadline})
    
    strategies = [
        {'name': 'Low-Error-Long', 'error': 1, 'duration': 35.0},
        {'name': 'Mid-Error-Mid', 'error': 3, 'duration': 18.0},
        {'name': 'High-Error-Short', 'error': 6, 'duration': 8.0},
    ]
    
    # Carbon intensity decreases over time
    carbon = [100, 95, 90, 80, 70, 60, 50, 40]
    
    # No capacity tiers for simplicity
    capacity_tiers = []
    
    # Print test instance
    print(f"\nTest Instance:")
    print(f"  Blocks: {num_blocks}")
    print(f"  Total Requests: {sum(b['size'] for b in blocks)}")
    print(f"  Time Slots: {delta}")
    print(f"  Slot Capacity: {slot_duration_minutes} × {parallelism} = {slot_duration_minutes * parallelism} min")
    print(f"  Error Threshold: {error_threshold}%")
    print(f"  Strategies: {len(strategies)}")
    for s in strategies:
        print(f"    - {s['name']}: error={s['error']}%, duration={s['duration']} min")
    print(f"  Carbon Intensity: {carbon}")
    
    results = []
    
    # 1. Greedy
    print_header("1. GREEDY CONTIGUOUS")
    start = time.time()
    cost_g, error_g, assign_g, resid_g = greedy_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers
    )
    time_g = time.time() - start
    
    if cost_g:
        print(f"✅ Cost: {cost_g:.2f}, Error: {error_g:.2f}%, Time: {time_g:.3f}s")
        print_assignment_distribution(assign_g, carbon, delta)
        results.append(('Greedy', cost_g, error_g, time_g, None))
    else:
        print("❌ Failed")
        results.append(('Greedy', None, None, time_g, None))
    
    # 2. First-Fit
    print_header("2. FIRST-FIT CONTIGUOUS")
    start = time.time()
    cost_ff, error_ff, assign_ff, resid_ff = first_fit_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers
    )
    time_ff = time.time() - start
    
    if cost_ff:
        print(f"✅ Cost: {cost_ff:.2f}, Error: {error_ff:.2f}%, Time: {time_ff:.3f}s")
        print_assignment_distribution(assign_ff, carbon, delta)
        results.append(('First-Fit', cost_ff, error_ff, time_ff, None))
    else:
        print("❌ Failed")
        results.append(('First-Fit', None, None, time_ff, None))
    
    # 3. Best-Fit
    print_header("3. BEST-FIT CONTIGUOUS")
    start = time.time()
    cost_bf, error_bf, assign_bf, resid_bf = best_fit_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers
    )
    time_bf = time.time() - start
    
    if cost_bf:
        print(f"✅ Cost: {cost_bf:.2f}, Error: {error_bf:.2f}%, Time: {time_bf:.3f}s")
        print_assignment_distribution(assign_bf, carbon, delta)
        results.append(('Best-Fit', cost_bf, error_bf, time_bf, None))
    else:
        print("❌ Failed")
        results.append(('Best-Fit', None, None, time_bf, None))
    
    # 4. Probabilistic Slack
    print_header("4. PROBABILISTIC SLACK")
    start = time.time()
    cost_ps, error_ps, assign_ps, resid_ps = probabilistic_slack_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers
    )
    time_ps = time.time() - start
    
    if cost_ps:
        print(f"✅ Cost: {cost_ps:.2f}, Error: {error_ps:.2f}%, Time: {time_ps:.3f}s")
        print_assignment_distribution(assign_ps, carbon, delta)
        results.append(('ProbSlack', cost_ps, error_ps, time_ps, None))
    else:
        print("❌ Failed")
        results.append(('ProbSlack', None, None, time_ps, None))
    
    # 5. DP with K-Best Pruning
    print_header("5. DP + K-BEST PRUNING")
    upper_bound = cost_g if cost_g else None
    start = time.time()
    cost_dp_kb, error_dp_kb, assign_dp_kb, resid_dp_kb, states_kb = dp_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers,
        upper_bound=upper_bound, pruning='kbest', pruning_k=10000, granularity=5
    )
    time_dp_kb = time.time() - start
    
    if cost_dp_kb:
        print(f"✅ Cost: {cost_dp_kb:.2f}, Error: {error_dp_kb:.2f}%, "
              f"Time: {time_dp_kb:.3f}s, States: {states_kb:,}")
        print_assignment_distribution(assign_dp_kb, carbon, delta)
        results.append(('DP+KBest', cost_dp_kb, error_dp_kb, time_dp_kb, states_kb))
    else:
        print("❌ Failed")
        results.append(('DP+KBest', None, None, time_dp_kb, states_kb))
    
    # 6. DP with Beam Search
    print_header("6. DP + BEAM SEARCH")
    start = time.time()
    cost_dp_b, error_dp_b, assign_dp_b, resid_dp_b, states_b = dp_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers,
        upper_bound=upper_bound, pruning='beam', pruning_k=150, granularity=5
    )
    time_dp_b = time.time() - start
    
    if cost_dp_b:
        print(f"✅ Cost: {cost_dp_b:.2f}, Error: {error_dp_b:.2f}%, "
              f"Time: {time_dp_b:.3f}s, States: {states_b:,}")
        print_assignment_distribution(assign_dp_b, carbon, delta)
        results.append(('DP+Beam', cost_dp_b, error_dp_b, time_dp_b, states_b))
    else:
        print("❌ Failed")
        results.append(('DP+Beam', None, None, time_dp_b, states_b))
    
    # Summary
    print_header("SUMMARY")
    
    # Find best cost
    valid_costs = [r[1] for r in results if r[1] is not None]
    best_cost = min(valid_costs) if valid_costs else None
    
    print(f"\n{'Method':<20} {'Cost':>12} {'Error':>8} {'Gap':>8} {'Time':>10} {'States':>12}")
    print("-" * 80)
    
    for name, cost, error, elapsed, states in results:
        if cost is None:
            print(f"{name:<20} {'FAILED':>12} {'-':>8} {'-':>8} {'-':>10} {'-':>12}")
        else:
            gap = ((cost - best_cost) / best_cost * 100) if best_cost else 0
            is_best = (cost == best_cost)
            marker = "🏆" if is_best else "  "
            states_str = f"{states:,}" if states else "-"
            print(f"{marker}{name:<18} {cost:>12,.2f} {error:>7.2f}% {gap:>7.1f}% "
                  f"{elapsed:>9.3f}s {states_str:>12}")
    
    print("\n🏆 = Optimal solution (lowest cost)")
    
    # Key insights
    print_header("KEY INSIGHTS")
    
    if cost_g and cost_dp_kb and cost_dp_b:
        if cost_dp_kb < cost_g or cost_dp_b < cost_g:
            best_dp_cost = min(cost_dp_kb, cost_dp_b)
            improvement = (cost_g - best_dp_cost) / cost_g * 100
            print(f"\n✅ DP found better solution than Greedy!")
            print(f"   Improvement: {improvement:.1f}%")
        elif cost_dp_kb == cost_g:
            print(f"\n📊 DP matched Greedy optimality")
        
        if states_kb:
            print(f"\n⚡ K-Best Pruning explored {states_kb:,} states")
        if states_b:
            print(f"⚡ Beam Search explored {states_b:,} states")


if __name__ == '__main__':
    run_comparison()
