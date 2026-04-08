#!/usr/bin/env python3
"""
Greedy Time-Aware Scheduler with Capacity Tiers

Fast greedy scheduler that considers:
- Carbon intensity per slot
- Execution duration per strategy
- Time capacity of slots (duration × parallelism)
- Capacity tiers (emission factor based on load)

Strategy: For each block, choose (slot, strategy) with minimum cost
that respects temporal constraints.
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


def calculate_slot_time_used(assignments, slot, strategies):
    """
    Calculate total time used in a slot given current assignments.
    
    Time = sum of durations of all assigned requests (they execute in parallel)
    With parallelism P, actual time = ceil(N / P) * max_duration_in_round
    
    For simplicity, we use: time = sum(durations) / parallelism
    """
    total_duration = 0
    for req_idx, strat_idx, t in assignments:
        if t == slot:
            total_duration += strategies[strat_idx]['duration']
    
    return total_duration


def greedy_time_aware(blocks, strategies, carbon, delta, error_threshold,
                     slot_duration_minutes, parallelism,
                     capacity_tiers=None):
    """
    Greedy time-aware scheduler with capacity tiers.
    
    Args:
        blocks: List of request blocks
        strategies: List of strategy dicts (error, duration)
        carbon: Carbon intensity per slot
        delta: Number of time slots
        error_threshold: Max average error
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Number of parallel executions per slot
        capacity_tiers: Capacity tier configuration
    
    Returns:
        assignment, cost, error, loads, time_used
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    B = len(blocks)
    slot_loads = [0] * delta
    slot_time_used = [0.0] * delta  # Time used per slot
    cumulative_error = 0
    total_cost = 0.0
    assignment = []
    
    # Slot time capacity (minutes)
    slot_time_capacity = slot_duration_minutes * parallelism
    
    # Deadline per blocco
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    for b in range(B):
        block_size = len(blocks[b])
        deadline = block_deadlines[b]
        
        # Try all (slot, strategy) combinations
        best_slot = None
        best_strategy = None
        best_cost = float('inf')
        
        for s, strategy in enumerate(strategies):
            # Check error constraint
            new_error = cumulative_error + strategy['error']
            avg_error = new_error / (b + 1)
            if avg_error > error_threshold:
                continue
            
            for t in range(min(deadline + 1, delta)):
                # Check time capacity constraint
                new_time_used = slot_time_used[t] + (strategy['duration'] * block_size / parallelism)
                
                if new_time_used > slot_time_capacity:
                    # Slot would overflow - skip
                    continue
                
                # Calculate cost with lookahead on emission factor
                new_load = slot_loads[t] + block_size
                emission_factor = get_emission_factor(new_load, capacity_tiers)
                
                # Cost = carbon × duration × block_size × emission_factor
                cost = carbon[t] * strategy['duration'] * block_size * emission_factor
                
                if cost < best_cost:
                    best_cost = cost
                    best_slot = t
                    best_strategy = s
        
        # Assign to best found
        if best_slot is None:
            # No feasible assignment - use first slot and first strategy as fallback
            print(f"Warning: Block {b} has no feasible assignment, using fallback", file=sys.stderr)
            best_slot = 0
            best_strategy = 0
            best_cost = carbon[0] * strategies[0]['duration'] * block_size
        
        slot_loads[best_slot] += block_size
        slot_time_used[best_slot] += strategies[best_strategy]['duration'] * block_size / parallelism
        cumulative_error += strategies[best_strategy]['error']
        total_cost += best_cost
        assignment.append((b, best_strategy, best_slot))
    
    avg_error = cumulative_error / B if B > 0 else 0
    
    return assignment, total_cost, avg_error, slot_loads, slot_time_used


def main():
    """Command line entry point"""
    
    if len(sys.argv) < 10:
        print("Usage: greedy_time.py <requests.csv> <strategies.csv> <carbon.txt> "
              "<delta> <beta> <error> <slot_duration_min> <parallelism> [output.csv] [--capacity-file <file>]",
              file=sys.stderr)
        sys.exit(1)
    
    # Parse arguments
    requests_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    delta = int(sys.argv[4])
    beta = int(sys.argv[5])
    error_threshold = float(sys.argv[6])
    slot_duration = int(sys.argv[7])
    parallelism = int(sys.argv[8])
    
    output_file = sys.argv[9] if len(sys.argv) > 9 and not sys.argv[9].startswith('--') else None
    
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
                'name': row.get('strategy', row.get('name', 'strategy'))
            })
    
    with open(carbon_file, 'r') as f:
        carbon = [float(line.strip()) for line in f if line.strip()]
    
    # Create blocks
    requests = [{'id': i, 'deadline': deadlines[i]} for i in range(len(deadlines))]
    block_size = len(requests) // beta
    blocks = [requests[i*block_size:(i+1)*block_size] for i in range(beta)]
    
    # Run scheduler
    assignment, total_cost, avg_error, slot_loads, time_used = greedy_time_aware(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration, parallelism, capacity_tiers
    )
    
    # Output
    print(f"Greedy Time-Aware solution:", file=sys.stderr)
    print(f"  Cost: {total_cost:.2f}", file=sys.stderr)
    print(f"  Error: {avg_error:.2f}%", file=sys.stderr)
    print(f"  Slot duration: {slot_duration} min", file=sys.stderr)
    print(f"  Parallelism: {parallelism}", file=sys.stderr)
    print(f"  Time capacity per slot: {slot_duration * parallelism} request-minutes", file=sys.stderr)
    print(f"  Loads: {slot_loads}", file=sys.stderr)
    print(f"  Time used: {[f'{t:.1f}' for t in time_used]}", file=sys.stderr)
    
    # Check for overflows
    slot_capacity = slot_duration * parallelism
    overflows = [(i, time_used[i], slot_capacity) for i in range(len(time_used)) 
                 if time_used[i] > slot_capacity]
    if overflows:
        print(f"  WARNING: Time capacity exceeded in {len(overflows)} slots:", file=sys.stderr)
        for slot, used, cap in overflows[:5]:
            print(f"    Slot {slot}: {used:.1f}/{cap} min", file=sys.stderr)
    
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
    print(f"MAX_TIME: {max(time_used) if time_used else 0}")
    print(f"LOADS: {','.join(map(str, slot_loads))}")
    print(f"TIMES: {','.join(f'{t:.2f}' for t in time_used)}")


if __name__ == '__main__':
    main()
