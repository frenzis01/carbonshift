#!/usr/bin/env python3
"""
ProbabilisticSlack Scheduler with Capacity Tiers

Online scheduler that uses deadline slack to decide when to postpone
requests to greener time slots, with capacity tier awareness.
"""

import sys
import csv
from collections import defaultdict


def load_capacity_tiers(filepath):
    """Load capacity tiers from CSV file"""
    tiers = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                capacity = int(parts[0])
                factor = float(parts[1])
                tiers.append({'capacity': capacity, 'factor': factor})
    
    tiers.sort(key=lambda x: x['capacity'])
    return tiers


def get_emission_factor(load, tiers):
    """Calculate emission factor based on load and tiers"""
    if not tiers:
        return 1.0
    
    for tier in tiers:
        if load <= tier['capacity']:
            return tier['factor']
    
    return tiers[-1]['factor']


def probabilistic_slack(blocks, strategies, carbon, delta, error_threshold, 
                       capacity_tiers=None, slack_threshold=3):
    """
    ProbabilisticSlack: postpone to green slots when deadline allows.
    
    Strategy:
    - If deadline is far (slack >= threshold) AND error budget available:
        → Postpone to greenest available slot with low-error strategy
    - Otherwise:
        → Schedule to nearest green slot
    
    Args:
        blocks: List of request blocks
        strategies: List of strategy dicts
        carbon: Carbon intensity per slot
        delta: Number of time slots
        error_threshold: Max average error
        capacity_tiers: Capacity tier configuration
        slack_threshold: Minimum slack for postponing
    
    Returns:
        assignment, cost, error, loads
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    B = len(blocks)
    slot_loads = [0] * delta
    cumulative_error = 0
    total_cost = 0.0
    assignment = []
    
    # Deadline per blocco
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # Sort strategies by error (low error = high quality)
    strategies_sorted = sorted(enumerate(strategies), key=lambda x: x[1]['error'])
    high_quality_idx = strategies_sorted[0][0]
    low_quality_idx = strategies_sorted[-1][0] if len(strategies) > 1 else high_quality_idx
    
    for b in range(B):
        block_size = len(blocks[b])
        deadline = block_deadlines[b]
        current_time = 0  # Assume all arrive at time 0
        
        slack = deadline - current_time
        
        # Check error budget remaining
        avg_error_so_far = cumulative_error / b if b > 0 else 0
        error_budget_left = error_threshold - avg_error_so_far
        
        # Decision: can we afford to use low-quality (postpone)?
        use_low_quality = (slack >= slack_threshold and 
                          error_budget_left > strategies[low_quality_idx]['error'])
        
        if use_low_quality:
            # Postpone to greenest slot
            strategy_idx = low_quality_idx
        else:
            # Use high quality
            strategy_idx = high_quality_idx
        
        # Find best slot within deadline
        best_slot = None
        best_cost = float('inf')
        
        for t in range(min(deadline + 1, delta)):
            new_load = slot_loads[t] + block_size
            emission_factor = get_emission_factor(new_load, capacity_tiers)
            cost = carbon[t] * strategies[strategy_idx]['duration'] * block_size * emission_factor
            
            if cost < best_cost:
                best_cost = cost
                best_slot = t
        
        # Assign
        if best_slot is None:
            best_slot = 0  # Fallback
        
        slot_loads[best_slot] += block_size
        cumulative_error += strategies[strategy_idx]['error']
        total_cost += best_cost
        assignment.append((b, strategy_idx, best_slot))
    
    avg_error = cumulative_error / B if B > 0 else 0
    
    return assignment, total_cost, avg_error, slot_loads


def main():
    """Command line entry point"""
    
    if len(sys.argv) < 7:
        print("Usage: probabilistic_slack.py <requests.csv> <strategies.csv> "
              "<carbon.txt> <delta> <beta> <error> [output.csv] [--capacity-file <file>]",
              file=sys.stderr)
        sys.exit(1)
    
    # Parse arguments
    requests_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    delta = int(sys.argv[4])
    beta = int(sys.argv[5])
    error_threshold = float(sys.argv[6])
    
    output_file = sys.argv[7] if len(sys.argv) > 7 and not sys.argv[7].startswith('--') else None
    
    # Load capacity tiers if specified
    capacity_tiers = None
    if '--capacity-file' in sys.argv:
        idx = sys.argv.index('--capacity-file')
        if idx + 1 < len(sys.argv):
            capacity_file = sys.argv[idx + 1]
            capacity_tiers = load_capacity_tiers(capacity_file)
            print(f"Loaded {len(capacity_tiers)} capacity tiers", file=sys.stderr)
    
    # Load inputs
    with open(requests_file, 'r') as f:
        deadlines = list(map(int, f.read().strip().split(',')))
    
    strategies = []
    with open(strategies_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            strategies.append({
                'error': float(row['error']),
                'duration': float(row['duration']),
                'name': row.get('name', 'strategy')
            })
    
    with open(carbon_file, 'r') as f:
        carbon = [float(line.strip()) for line in f if line.strip()]
    
    # Create blocks
    requests = [{'id': i, 'deadline': deadlines[i]} for i in range(len(deadlines))]
    block_size = len(requests) // beta
    blocks = [requests[i*block_size:(i+1)*block_size] for i in range(beta)]
    
    # Run scheduler
    assignment, total_cost, avg_error, slot_loads = probabilistic_slack(
        blocks, strategies, carbon, delta, error_threshold, capacity_tiers
    )
    
    # Output
    print(f"ProbabilisticSlack solution:", file=sys.stderr)
    print(f"  Cost: {total_cost:.2f}", file=sys.stderr)
    print(f"  Error: {avg_error:.2f}%", file=sys.stderr)
    print(f"  Loads: {slot_loads}", file=sys.stderr)
    
    # Write output if specified
    if output_file:
        with open(output_file, 'w') as f:
            f.write("request_id,strategy,slot\n")
            for block_idx, strategy_idx, slot in assignment:
                strategy_name = strategies[strategy_idx]['name']
                for req in blocks[block_idx]:
                    f.write(f"{req['id']},{strategy_name},{slot}\n")
            
            f.write(f"\nall_emissions: {total_cost}\n")
            f.write(f"all_errors: {avg_error}\n")
        
        print(f"Written to {output_file}", file=sys.stderr)
    
    # Print parseable output
    print(f"COST: {total_cost}")
    print(f"ERROR: {avg_error}")
    print(f"MAX_LOAD: {max(slot_loads) if slot_loads else 0}")
    print(f"LOADS: {','.join(map(str, slot_loads))}")


if __name__ == '__main__':
    main()
