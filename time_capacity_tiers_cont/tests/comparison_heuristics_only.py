#!/usr/bin/env python3
"""
Comprehensive comparison of contiguous scheduling heuristics.

Compares all heuristics (without DP which has bugs to fix).
"""

import sys
import time
import random
sys.path.insert(0, '..')

from greedy_cont import greedy_contiguous
from first_fit_cont import first_fit_contiguous
from best_fit_cont import best_fit_contiguous
from probabilistic_slack_cont import probabilistic_slack_contiguous
from utils_cont import validate_solution


def generate_test_instance(num_blocks, num_slots, seed=42):
    """Generate a test instance"""
    random.seed(seed)
    
    blocks = []
    for i in range(num_blocks):
        size = random.randint(2, 6)
        deadline = random.randint(num_slots // 2 + 2, num_slots - 1)
        blocks.append({'size': size, 'deadline': deadline})
    
    # Error as INTEGER (e.g., 3 = 3%)
    strategies = [
        {'name': 'High', 'error': 1, 'duration': 25.0},
        {'name': 'Medium', 'error': 3, 'duration': 15.0},
        {'name': 'Low', 'error': 6, 'duration': 8.0},
    ]
    
    carbon = []
    base = 150.0
    for i in range(num_slots):
        carbon.append(base * (1.0 - 0.6 * i / num_slots) + random.uniform(-10, 10))
    
    capacity_tiers = [
        {'capacity': 5, 'factor': 1.0},
        {'capacity': 10, 'factor': 2.0},
        {'capacity': 15, 'factor': 4.0},
        {'capacity': 25, 'factor': 8.0},
    ]
    
    return blocks, strategies, carbon, capacity_tiers


def analyze_solution(assignments, blocks, strategies, residuals, 
                     slot_duration_minutes, parallelism):
    """Analyze solution quality"""
    if assignments is None:
        return None
    
    capacity_per_slot = slot_duration_minutes * parallelism
    total_capacity = len(residuals) * capacity_per_slot
    total_used = sum(capacity_per_slot - r for r in residuals)
    utilization = (total_used / total_capacity) * 100
    
    slots_used = set()
    for _, _, start_slot, slots_info in assignments:
        for slot_idx, _, _ in slots_info:
            slots_used.add(slot_idx)
    
    total_span = 0
    for _, _, start_slot, slots_info in assignments:
        if len(slots_info) > 0:
            last_slot = max(s[0] for s in slots_info)
            span = last_slot - start_slot + 1
            total_span += span
    avg_span = total_span / len(assignments) if assignments else 0
    
    return {
        'utilization': utilization,
        'slots_used': len(slots_used),
        'avg_span': avg_span,
    }


def main():
    print("=" * 80)
    print("CONTIGUOUS TIME MODEL - HEURISTICS COMPARISON")
    print("=" * 80)
    
    NUM_BLOCKS = 10
    NUM_SLOTS = 10
    ERROR_THRESHOLD = 5  # Integer: 5 = 5%
    SLOT_DURATION_MIN = 30
    PARALLELISM = 3
    SEED = 42
    
    print(f"\nConfiguration:")
    print(f"  Blocks: {NUM_BLOCKS}")
    print(f"  Slots: {NUM_SLOTS} × {SLOT_DURATION_MIN} min")
    print(f"  Parallelism: {PARALLELISM}")
    print(f"  Error threshold: {ERROR_THRESHOLD}%")
    print(f"  Slot capacity: {SLOT_DURATION_MIN * PARALLELISM} req-min each")
    
    blocks, strategies, carbon, capacity_tiers = generate_test_instance(
        NUM_BLOCKS, NUM_SLOTS, SEED
    )
    
    print(f"\nGenerated instance:")
    print(f"  Total requests: {sum(b['size'] for b in blocks)}")
    print(f"  Strategies: {len(strategies)}")
    for i, s in enumerate(strategies):
        print(f"    {i}. {s['name']}: {s['duration']} min, {s['error']}% error")
    
    print("\n" + "=" * 80)
    print("RUNNING SCHEDULERS")
    print("=" * 80)
    
    results = {}
    
    # 1. Greedy
    print("\n[1/4] Running Greedy Contiguous...")
    start = time.time()
    cost_g, error_g, assign_g, resid_g = greedy_contiguous(
        blocks, strategies, carbon, NUM_SLOTS, ERROR_THRESHOLD,
        SLOT_DURATION_MIN, PARALLELISM, capacity_tiers
    )
    time_g = time.time() - start
    
    if cost_g is not None:
        valid_g, _ = validate_solution(assign_g, blocks, strategies, NUM_SLOTS,
                                       ERROR_THRESHOLD, SLOT_DURATION_MIN, PARALLELISM)
        analysis_g = analyze_solution(assign_g, blocks, strategies, resid_g,
                                      SLOT_DURATION_MIN, PARALLELISM)
        results['Greedy'] = {
            'cost': cost_g, 'error': error_g, 'time': time_g, 'valid': valid_g,
            'analysis': analysis_g
        }
        print(f"  ✓ Cost: {cost_g:.2f}, Error: {error_g:.2f}%, Time: {time_g:.3f}s")
    else:
        results['Greedy'] = None
        print(f"  ✗ FAILED")
    
    # 2. First-Fit
    print("\n[2/4] Running First-Fit Contiguous...")
    start = time.time()
    cost_f, error_f, assign_f, resid_f = first_fit_contiguous(
        blocks, strategies, carbon, NUM_SLOTS, ERROR_THRESHOLD,
        SLOT_DURATION_MIN, PARALLELISM, capacity_tiers
    )
    time_f = time.time() - start
    
    if cost_f is not None:
        valid_f, _ = validate_solution(assign_f, blocks, strategies, NUM_SLOTS,
                                       ERROR_THRESHOLD, SLOT_DURATION_MIN, PARALLELISM)
        analysis_f = analyze_solution(assign_f, blocks, strategies, resid_f,
                                      SLOT_DURATION_MIN, PARALLELISM)
        results['First-Fit'] = {
            'cost': cost_f, 'error': error_f, 'time': time_f, 'valid': valid_f,
            'analysis': analysis_f
        }
        print(f"  ✓ Cost: {cost_f:.2f}, Error: {error_f:.2f}%, Time: {time_f:.3f}s")
    else:
        results['First-Fit'] = None
        print(f"  ✗ FAILED")
    
    # 3. Best-Fit
    print("\n[3/4] Running Best-Fit Contiguous...")
    start = time.time()
    cost_b, error_b, assign_b, resid_b = best_fit_contiguous(
        blocks, strategies, carbon, NUM_SLOTS, ERROR_THRESHOLD,
        SLOT_DURATION_MIN, PARALLELISM, capacity_tiers
    )
    time_b = time.time() - start
    
    if cost_b is not None:
        valid_b, _ = validate_solution(assign_b, blocks, strategies, NUM_SLOTS,
                                       ERROR_THRESHOLD, SLOT_DURATION_MIN, PARALLELISM)
        analysis_b = analyze_solution(assign_b, blocks, strategies, resid_b,
                                      SLOT_DURATION_MIN, PARALLELISM)
        results['Best-Fit'] = {
            'cost': cost_b, 'error': error_b, 'time': time_b, 'valid': valid_b,
            'analysis': analysis_b
        }
        print(f"  ✓ Cost: {cost_b:.2f}, Error: {error_b:.2f}%, Time: {time_b:.3f}s")
    else:
        results['Best-Fit'] = None
        print(f"  ✗ FAILED")
    
    # 4. Probabilistic Slack
    print("\n[4/4] Running Probabilistic Slack...")
    start = time.time()
    cost_p, error_p, assign_p, resid_p = probabilistic_slack_contiguous(
        blocks, strategies, carbon, NUM_SLOTS, ERROR_THRESHOLD,
        SLOT_DURATION_MIN, PARALLELISM, capacity_tiers, slack_threshold=3, seed=SEED
    )
    time_p = time.time() - start
    
    if cost_p is not None:
        valid_p, _ = validate_solution(assign_p, blocks, strategies, NUM_SLOTS,
                                       ERROR_THRESHOLD, SLOT_DURATION_MIN, PARALLELISM)
        analysis_p = analyze_solution(assign_p, blocks, strategies, resid_p,
                                      SLOT_DURATION_MIN, PARALLELISM)
        results['ProbSlack'] = {
            'cost': cost_p, 'error': error_p, 'time': time_p, 'valid': valid_p,
            'analysis': analysis_p
        }
        print(f"  ✓ Cost: {cost_p:.2f}, Error: {error_p:.2f}%, Time: {time_p:.3f}s")
    else:
        results['ProbSlack'] = None
        print(f"  ✗ FAILED")
    
    valid_results = {k: v for k, v in results.items() if v is not None}
    if not valid_results:
        print("\n⚠️  No valid solutions found!")
        return
    
    best_cost = min(r['cost'] for r in valid_results.values())
    best_error = min(r['error'] for r in valid_results.values())
    
    print("\n" + "=" * 80)
    print("COMPARISON RESULTS")
    print("=" * 80)
    print()
    print(f"{'Method':<20} {'Cost':>12} {'CostGap':>9} {'Error':>8} {'ErrGap':>9} {'Time':>8} {'Util':>8}")
    print("-" * 80)
    
    for name in ['Greedy', 'First-Fit', 'Best-Fit', 'ProbSlack']:
        if results[name] is None:
            print(f"{name:<20} {'FAILED':>12} {'-':>9} {'-':>8} {'-':>9} {'-':>8} {'-':>8}")
        else:
            r = results[name]
            cost_gap = ((r['cost'] - best_cost) / best_cost * 100) if best_cost > 0 else 0
            err_gap = ((r['error'] - best_error) / best_error * 100) if best_error > 0 else 0
            marker = "🏆" if abs(r['cost'] - best_cost) < 1e-6 else "  "
            
            print(f"{marker}{name:<18} {r['cost']:>12.2f} {cost_gap:>8.1f}% "
                  f"{r['error']:>7.2f}% {err_gap:>8.1f}% {r['time']:>7.2f}s "
                  f"{r['analysis']['utilization']:>7.1f}%")
    
    print()
    print("🏆 = Best cost")
    print()
    print("Note: DP implementations available but being debugged")
    print("      (see comparison_full.py for experimental DP version)")
    
    print("\n" + "=" * 80)
    print("✅ COMPARISON COMPLETE")
    print("=" * 80)


if __name__ == '__main__':
    main()
