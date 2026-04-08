#!/usr/bin/env python3
"""
Probabilistic Slack Scheduler with Contiguous Spanning

Online scheduler that uses deadline slack to decide when to postpone
requests to greener time slots, with contiguous spanning support.
"""

import sys
import random
from utils_cont import (
    load_capacity_tiers,
    try_assign_contiguous,
    apply_assignment,
    calculate_initial_residuals,
    validate_solution
)


def probabilistic_slack_contiguous(blocks, strategies, carbon, delta, error_threshold,
                                   slot_duration_minutes, parallelism, capacity_tiers=None,
                                   slack_threshold=3, seed=42):
    """
    Probabilistic slack scheduler with contiguous spanning.
    
    Strategy:
    - If deadline is far (slack >= threshold) AND error budget available:
        → Probabilistically postpone to greener slot with low-error strategy
    - Otherwise:
        → Schedule to greenest available slot
    
    Args:
        blocks: List of dicts with 'size' and 'deadline'
        strategies: List of dicts with 'error' and 'duration'
        carbon: Carbon intensity per slot
        delta: Number of time slots
        error_threshold: Max average error (integer, e.g., 5 for 5%)
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Degree of parallelism
        capacity_tiers: Capacity tier configuration
        slack_threshold: Minimum slack for postponing
        seed: Random seed
    
    Returns:
        (cost, avg_error, assignments, residuals)
    """
    
    if capacity_tiers is None:
        capacity_tiers = []
    
    random.seed(seed)
    
    # Initialize
    residuals = calculate_initial_residuals(delta, slot_duration_minutes, parallelism)
    assignments = []
    total_cost = 0.0
    cumulative_error = 0
    cumulative_requests = 0
    
    # Sort strategies by error (low error = high quality)
    # Also consider duration - we want low error AND short duration for postponing
    strategies_sorted = sorted(enumerate(strategies), key=lambda x: x[1]['error'])
    high_quality_idx = strategies_sorted[0][0]  # Lowest error
    
    # For postponing, use high-error LOW-duration (faster execution)
    # This allows more requests to fit in green slots
    strategies_by_duration = sorted(enumerate(strategies), key=lambda x: x[1]['duration'])
    fast_strategy_idx = strategies_by_duration[0][0]  # Shortest duration
    
    # Process blocks in order
    for block_idx, block in enumerate(blocks):
        block_size = block['size']
        deadline = block['deadline']
        current_time = 0  # Assume all arrive at time 0
        
        slack = deadline - current_time
        
        # Check error budget remaining
        avg_error_so_far = cumulative_error / cumulative_requests if cumulative_requests > 0 else 0
        error_budget_left = error_threshold - avg_error_so_far
        
        # Decision: can we afford to use fast strategy (postpone)?
        # Use fast strategy if we have slack AND can tolerate the error
        can_postpone = (slack >= slack_threshold and 
                       error_budget_left > strategies[fast_strategy_idx]['error'])
        
        # Probabilistic decision based on slack
        if can_postpone:
            # Probability of postponing increases with slack
            postpone_prob = min(0.8, slack / (delta - current_time))
            use_low_quality = random.random() < postpone_prob
        else:
            use_low_quality = False
        
        # Choose strategy
        if use_low_quality:
            # Use fast (low duration) strategy for postponing
            strategy_idx = fast_strategy_idx
            target_strategy = strategies[fast_strategy_idx]
        else:
            # Use high quality (low error) strategy
            strategy_idx = high_quality_idx
            target_strategy = strategies[high_quality_idx]
        
        # Check if this strategy fits error budget
        potential_error = cumulative_error + target_strategy['error'] * block_size
        potential_requests = cumulative_requests + block_size
        potential_avg_error = potential_error / potential_requests
        
        if potential_avg_error > error_threshold:
            # Fall back to high-quality strategy
            strategy_idx = high_quality_idx
            target_strategy = strategies[high_quality_idx]
            
            # Re-check
            potential_error = cumulative_error + target_strategy['error'] * block_size
            potential_avg_error = potential_error / potential_requests
            
            if potential_avg_error > error_threshold:
                print(f"ERROR: No strategy respects error budget for block {block_idx}",
                      file=sys.stderr)
                return None, None, None, None
        

        
        # Find best slot (greenest with capacity)
        best_cost = float('inf')
        best_start_slot = None
        best_slots_used = None
        best_time_per_slot = None
        
        # If postponing, prefer later (greener) slots
        if use_low_quality:
            # Try slots in reverse order (latest first)
            slot_order = list(range(deadline + 1))[::-1]
        else:
            # Try slots in order (earliest first for reliability)
            slot_order = list(range(deadline + 1))
        
        for start_slot in slot_order:
            cost, feasible, slots_used, time_per_slot = try_assign_contiguous(
                block_size=block_size,
                strategy_duration=target_strategy['duration'],
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
                best_start_slot = start_slot
                best_slots_used = slots_used
                best_time_per_slot = time_per_slot
                
                # If postponing, take first feasible green slot
                if use_low_quality:
                    break
        
        # Check if we found a feasible assignment
        if best_start_slot is None:
            # If failed, try other strategies as fallback
            print(f"WARNING: Strategy {strategy_idx} ({strategies[strategy_idx]['name']}) "
                  f"failed for block {block_idx}. Trying all strategies...", file=sys.stderr)
            
            for fallback_s_idx, fallback_s in enumerate(strategies):
                if fallback_s_idx == strategy_idx:
                    continue  # Already tried
                
                # Check error feasibility
                pot_err = cumulative_error + fallback_s['error'] * block_size
                pot_avg = pot_err / (cumulative_requests + block_size)
                if pot_avg > error_threshold:
                    continue
                
                # Try all slots
                for start_slot in range(deadline + 1):
                    cost, feasible, slots_used, time_per_slot = try_assign_contiguous(
                        block_size=block_size,
                        strategy_duration=fallback_s['duration'],
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
                        best_start_slot = start_slot
                        best_slots_used = slots_used
                        best_time_per_slot = time_per_slot
                        strategy_idx = fallback_s_idx
                        target_strategy = fallback_s
            
            # Final check
            if best_start_slot is None:
                print(f"ERROR: No feasible slot for block {block_idx} with any strategy", file=sys.stderr)
                return None, None, None, None
        
        # Commit assignment
        apply_assignment(residuals, best_time_per_slot)
        assignments.append((block_idx, strategy_idx, best_start_slot, best_slots_used))
        
        total_cost += best_cost
        cumulative_error += target_strategy['error'] * block_size
        cumulative_requests += block_size
    
    # Calculate final average error
    avg_error = cumulative_error / cumulative_requests if cumulative_requests > 0 else 0
    
    return total_cost, avg_error, assignments, residuals


def main():
    """Example usage"""
    if len(sys.argv) < 8:
        print("Usage: python probabilistic_slack_cont.py <blocks_csv> <strategies_csv> "
              "<carbon_csv> <capacity_tiers_csv> <error_threshold> <slot_duration> "
              "<parallelism> [--slack-threshold <n>] [--seed <n>]")
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
    slack_threshold = 3
    seed = 42
    
    i = 8
    while i < len(sys.argv):
        if sys.argv[i] == '--slack-threshold':
            slack_threshold = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--seed':
            seed = int(sys.argv[i + 1])
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
                'error': int(row['error']),  # Integer!
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
    
    # Run probabilistic slack
    cost, error, assignments, residuals = probabilistic_slack_contiguous(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration_minutes, parallelism, capacity_tiers,
        slack_threshold, seed
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
    
    # Print results
    print(f"\nProbabilistic Slack Contiguous Solution")
    print("=" * 80)
    print(f"Total Cost: {cost:.2f}")
    print(f"Average Error: {error:.4f} ({error*100:.2f}%)")
    print(f"Validation: ✅ PASS")


if __name__ == '__main__':
    main()
