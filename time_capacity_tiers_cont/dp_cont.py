#!/usr/bin/env python3
"""
DP Contiguous with K-Best and Beam Search Pruning

Simplified DP without "no pruning" mode - only K-Best and Beam variants.
"""

import sys
import time
from collections import defaultdict
from utils_cont import (
    load_capacity_tiers,
    try_assign_contiguous,
    calculate_initial_residuals,
    validate_solution
)


def discretize_time(time_minutes, granularity=5):
    """Convert continuous time to discrete units"""
    return max(0, int(time_minutes / granularity))


def undiscretize_time(time_units, granularity=5):
    """Convert discrete units back to minutes"""
    return time_units * granularity


def dp_contiguous(blocks, strategies, carbon, delta, error_threshold,
                 slot_duration_minutes, parallelism, capacity_tiers=None,
                 upper_bound=None, pruning='beam', pruning_k=150, granularity=5):
    """
    DP with contiguous spanning and pruning (K-Best or Beam).
    
    Args:
        blocks: List of dicts with 'size' and 'deadline'
        strategies: List of dicts with 'error' (integer) and 'duration'
        carbon: Carbon intensity per slot
        delta: Number of time slots
        error_threshold: Max average error (integer, e.g., 5 for 5%)
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Degree of parallelism
        capacity_tiers: Capacity tier configuration
        upper_bound: Upper bound on cost (from warm-start)
        pruning: 'kbest' or 'beam'
        pruning_k: Number of states to keep
        granularity: Time discretization granularity in minutes
    
    Returns:
        (cost, avg_error, assignments, residuals, states_explored)
    """
    
    if capacity_tiers is None:
        capacity_tiers = []
    
    if pruning not in ['kbest', 'beam']:
        raise ValueError(f"Pruning must be 'kbest' or 'beam', got '{pruning}'")
    
    B = len(blocks)
    total_requests = sum(b['size'] for b in blocks)
    
    # Discretize slot capacity
    slot_capacity_minutes = slot_duration_minutes * parallelism
    slot_capacity_units = discretize_time(slot_capacity_minutes, granularity)
    
    # DP state: D[block][(error, residual_times_tuple)] = cost
    D = [defaultdict(lambda: float('inf')) for _ in range(B + 1)]
    trace = [dict() for _ in range(B + 1)]
    
    # Initial state: all slots at full capacity
    initial_residuals = tuple([slot_capacity_units] * delta)
    D[0][(0, initial_residuals)] = 0.0
    
    states_explored = 0
    
    print(f"DP Contiguous ({pruning.upper()}, K={pruning_k}, gran={granularity}min)")
    
    # Process each block
    for b in range(B):
        block_size = blocks[b]['size']
        deadline = blocks[b]['deadline']
        
        # Get current states and apply pruning BEFORE exploring transitions
        current_states = [(key, cost) for key, cost in D[b].items() if cost < float('inf')]
        
        if not current_states:
            print(f"ERROR: No states at block {b}")
            return None, None, None, None, states_explored
        
        # Pruning strategies
        if pruning == 'kbest':
            # Keep top-K states globally by cost
            current_states.sort(key=lambda x: x[1])
            current_states = current_states[:pruning_k]
        
        elif pruning == 'beam':
            # Group by error, keep top-K per error level
            error_groups = defaultdict(list)
            for (e, res), cost in current_states:
                error_groups[e].append(((e, res), cost))
            
            pruned_states = []
            for e in sorted(error_groups.keys()):
                group = error_groups[e]
                group.sort(key=lambda x: x[1])
                # Keep top-K for this error level
                pruned_states.extend(group[:pruning_k])
            
            current_states = pruned_states
        
        if b % max(1, B // 5) == 0 or b == B - 1:
            print(f"  Block {b+1}/{B}: {len(current_states)} states")
        
        # Explore transitions from pruned states
        for (e_prev, residuals_prev), cost_prev in current_states:
            # Convert to minutes for simulation
            residuals_list = [undiscretize_time(u, granularity) for u in residuals_prev]
            
            # Try all strategies
            for s_idx, strategy in enumerate(strategies):
                new_error = e_prev + strategy['error'] * block_size
                
                # Check error feasibility
                if new_error > error_threshold * total_requests:
                    continue
                
                # Try all starting slots within deadline
                for start_slot in range(min(deadline + 1, delta)):
                    # Simulate contiguous assignment
                    cost_contribution, feasible, slots_used, time_per_slot = try_assign_contiguous(
                        block_size=block_size,
                        strategy_duration=strategy['duration'],
                        start_slot=start_slot,
                        residuals=residuals_list.copy(),
                        carbon=carbon,
                        capacity_tiers=capacity_tiers,
                        slot_duration_minutes=slot_duration_minutes,
                        parallelism=parallelism,
                        deadline_slot=deadline
                    )
                    
                    if not feasible:
                        continue
                    
                    # Calculate new cost
                    new_cost = cost_prev + cost_contribution
                    
                    # Warm-start pruning (if upper bound provided)
                    if upper_bound is not None and new_cost > upper_bound:
                        continue
                    
                    # Update residuals
                    new_residuals_list = residuals_list.copy()
                    for slot_idx in range(delta):
                        new_residuals_list[slot_idx] -= time_per_slot[slot_idx]
                    
                    # Discretize new residuals
                    new_residuals_discrete = tuple([
                        discretize_time(r, granularity) for r in new_residuals_list
                    ])
                    
                    # Update DP table
                    state_key = (new_error, new_residuals_discrete)
                    
                    if new_cost < D[b + 1][state_key]:
                        D[b + 1][state_key] = new_cost
                        trace[b + 1][state_key] = (e_prev, residuals_prev, s_idx, start_slot, slots_used)
                        states_explored += 1
    
    # Find best final state
    final_states = [(key, cost) for key, cost in D[B].items() if cost < float('inf')]
    
    if not final_states:
        print("ERROR: No feasible solution found")
        return None, None, None, None, states_explored
    
    # Find minimum cost solution
    best_state, best_cost = min(final_states, key=lambda x: x[1])
    best_error, best_residuals_discrete = best_state
    
    # Reconstruct solution
    assignments = []
    current_state = best_state
    
    for b in range(B, 0, -1):
        if current_state not in trace[b]:
            print(f"ERROR: Cannot reconstruct path at block {b}")
            return None, None, None, None, states_explored
        
        e_prev, res_prev, s_idx, start_slot, slots_used = trace[b][current_state]
        assignments.append((b - 1, s_idx, start_slot, slots_used))
        current_state = (e_prev, res_prev)
    
    assignments.reverse()
    
    # Convert final residuals back to minutes
    final_residuals = [undiscretize_time(u, granularity) for u in best_residuals_discrete]
    
    avg_error = best_error / total_requests if total_requests > 0 else 0
    
    print(f"DP Complete: Cost={best_cost:.2f}, Error={avg_error:.2f}%, States={states_explored:,}")
    
    return best_cost, avg_error, assignments, final_residuals, states_explored


def main():
    """Example usage"""
    if len(sys.argv) < 8:
        print("Usage: python dp_cont_fixed.py <blocks_csv> <strategies_csv> <carbon_csv> "
              "<capacity_tiers_csv> <error_threshold> <slot_duration> <parallelism> "
              "[--upper-bound <cost>] [--pruning <kbest|beam>] [--pruning-k <k>] [--granularity <min>]")
        sys.exit(1)
    
    # Parse arguments
    blocks_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    tiers_file = sys.argv[4]
    error_threshold = int(sys.argv[5])
    slot_duration_minutes = int(sys.argv[6])
    parallelism = int(sys.argv[7])
    
    # Optional arguments
    upper_bound = None
    pruning = 'beam'
    pruning_k = 150
    granularity = 5
    
    i = 8
    while i < len(sys.argv):
        if sys.argv[i] == '--upper-bound':
            upper_bound = float(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--pruning':
            pruning = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == '--pruning-k':
            pruning_k = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--granularity':
            granularity = int(sys.argv[i + 1])
            i += 2
        else:
            i += 1
    
    # Load inputs
    blocks = []
    with open(blocks_file, 'r') as f:
        import csv
        reader = csv.DictReader(f)
        for row in reader:
            blocks.append({
                'size': int(row['size']),
                'deadline': int(row['deadline'])
            })
    
    strategies = []
    with open(strategies_file, 'r') as f:
        import csv
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            strategies.append({
                'name': row.get('name', f'S{idx}'),
                'error': int(row['error']),
                'duration': float(row['duration'])
            })
    
    carbon = []
    with open(carbon_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                carbon.append(float(line))
    
    capacity_tiers = load_capacity_tiers(tiers_file) if tiers_file != 'none' else []
    
    delta = len(carbon)
    
    # Run DP
    start_time = time.time()
    cost, error, assignments, residuals, states = dp_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers,
        upper_bound, pruning, pruning_k, granularity
    )
    elapsed = time.time() - start_time
    
    if cost is None:
        print("No feasible solution found")
        sys.exit(1)
    
    # Validate
    valid, errors = validate_solution(
        assignments, blocks, strategies, delta, error_threshold,
        slot_duration_minutes, parallelism
    )
    
    if not valid:
        print("Solution validation FAILED:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    
    # Print results
    print(f"\nDP Contiguous Solution ({pruning.upper()})")
    print("=" * 80)
    print(f"Total Cost: {cost:.2f}")
    print(f"Average Error: {error:.2f}%")
    print(f"Runtime: {elapsed:.3f}s")
    print(f"States Explored: {states:,}")
    print(f"Validation: ✅ PASS")


if __name__ == '__main__':
    main()
