#!/usr/bin/env python3
"""
Greedy Contiguous Scheduler

Assigns blocks sequentially, choosing (start_slot, strategy) with minimum cost.
Allows spanning across contiguous time slots.
"""

import sys
from utils_cont import (
    load_capacity_tiers,
    try_assign_contiguous,
    apply_assignment,
    calculate_initial_residuals,
    format_solution,
    validate_solution
)


def greedy_contiguous(blocks, strategies, carbon, delta, error_threshold,
                     slot_duration_minutes, parallelism, capacity_tiers=None):
    """
    Greedy scheduler with contiguous time slot spanning.
    
    For each block, finds (start_slot, strategy) with minimum cost that:
    - Respects error budget
    - Fits within available capacity (possibly spanning multiple slots)
    - Respects deadline
    
    Args:
        blocks: List of dicts with 'size' and 'deadline'
        strategies: List of dicts with 'error' and 'duration'
        carbon: List of carbon intensities per slot
        delta: Number of time slots
        error_threshold: Maximum average error allowed
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Number of parallel executions
        capacity_tiers: Capacity tier configuration (list of dicts)
    
    Returns:
        (total_cost, total_error, assignments, residuals)
    """
    if capacity_tiers is None:
        capacity_tiers = []
    
    # Initialize residual capacities
    residuals = calculate_initial_residuals(delta, slot_duration_minutes, parallelism)
    
    # Track assignments
    assignments = []
    total_cost = 0.0
    cumulative_error = 0.0
    cumulative_requests = 0
    
    # Process blocks in order
    for block_idx, block in enumerate(blocks):
        block_size = block['size']
        deadline = block['deadline']
        
        # Find best (start_slot, strategy) assignment
        best_cost = float('inf')
        best_strategy_idx = None
        best_start_slot = None
        best_slots_used = None
        best_time_per_slot = None
        
        for strategy_idx, strategy in enumerate(strategies):
            # Check if this strategy respects error budget
            potential_error = cumulative_error + strategy['error'] * block_size
            potential_avg_error = potential_error / (cumulative_requests + block_size)
            
            if potential_avg_error > error_threshold:
                continue
            
            # Try all possible starting slots
            for start_slot in range(deadline + 1):
                # Try to assign starting from this slot
                cost, feasible, slots_used, time_per_slot = try_assign_contiguous(
                    block_size=block_size,
                    strategy_duration=strategy['duration'],
                    start_slot=start_slot,
                    residuals=residuals,
                    carbon=carbon,
                    capacity_tiers=capacity_tiers,
                    slot_duration_minutes=slot_duration_minutes,
                    parallelism=parallelism,
                    deadline_slot=deadline
                )
                
                if feasible and cost < best_cost:
                    best_cost = cost
                    best_strategy_idx = strategy_idx
                    best_start_slot = start_slot
                    best_slots_used = slots_used
                    best_time_per_slot = time_per_slot
        
        # Check if we found a feasible assignment
        if best_strategy_idx is None:
            # No feasible assignment found
            print(f"ERROR: No feasible assignment for block {block_idx}", file=sys.stderr)
            return None, None, None, None
        
        # Commit the best assignment
        apply_assignment(residuals, best_time_per_slot)
        assignments.append((block_idx, best_strategy_idx, best_start_slot, best_slots_used))
        
        total_cost += best_cost
        cumulative_error += strategies[best_strategy_idx]['error'] * block_size
        cumulative_requests += block_size
    
    # Calculate final average error
    avg_error = cumulative_error / cumulative_requests if cumulative_requests > 0 else 0
    
    return total_cost, avg_error, assignments, residuals


def main():
    """Example usage"""
    if len(sys.argv) < 7:
        print("Usage: python greedy_cont.py <blocks_csv> <strategies_csv> <carbon_csv> "
              "<capacity_tiers_csv> <error_threshold> <slot_duration> <parallelism>")
        sys.exit(1)
    
    blocks_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    tiers_file = sys.argv[4]
    error_threshold = float(sys.argv[5])
    slot_duration_minutes = int(sys.argv[6])
    parallelism = int(sys.argv[7])
    
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
                'error': float(row['error']),
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
    
    # Run greedy
    cost, error, assignments, residuals = greedy_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers
    )
    
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
    
    # Format and print results
    solution = format_solution(
        assignments, strategies, blocks, carbon, capacity_tiers,
        slot_duration_minutes, parallelism
    )
    
    print(f"\nGreedy Contiguous Solution")
    print("=" * 80)
    print(f"Total Cost: {solution['cost']:.2f}")
    print(f"Total Error: {solution['error']:.4f}")
    print(f"Average Error: {solution['error'] / sum(b['size'] for b in blocks):.4f}")
    print()
    print("Slot Usage:")
    for slot_idx in sorted(solution['slot_details'].keys()):
        details = solution['slot_details'][slot_idx]
        capacity = slot_duration_minutes * parallelism
        utilization = (details['time_used'] / capacity) * 100
        print(f"  Slot {slot_idx:2d}: {details['time_used']:6.2f}/{capacity:6.2f} min ({utilization:5.1f}% full)")
        print(f"            Carbon: {carbon[slot_idx]:6.2f}")
        for block_info in details['blocks']:
            print(f"            - Block {block_info['block_idx']} ({block_info['strategy']}): {block_info['time']:.2f} min")
    
    print()
    print("Residual Capacity:")
    for slot_idx, residual in enumerate(residuals):
        capacity = slot_duration_minutes * parallelism
        print(f"  Slot {slot_idx:2d}: {residual:6.2f}/{capacity:6.2f} min remaining")


if __name__ == '__main__':
    main()
