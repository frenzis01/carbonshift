#!/usr/bin/env python3
"""
First-Fit Time-Aware Scheduler

Simple heuristic: For each block, use the first (slot, strategy) pair
that satisfies all constraints (deadline, error, time capacity).

Fast but may not find optimal solution.
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


def first_fit_time_aware(blocks, strategies, carbon, delta, error_threshold,
                         slot_duration_minutes, parallelism,
                         capacity_tiers=None):
    """
    First-fit time-aware scheduler.
    
    Strategy: For each block, try slots in order until one fits.
    Within each slot, try strategies by quality (low error first).
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    B = len(blocks)
    slot_loads = [0] * delta
    slot_time_used = [0.0] * delta
    cumulative_error = 0
    total_cost = 0.0
    assignment = []
    
    slot_time_capacity = slot_duration_minutes * parallelism
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # Sort strategies by error (low error = high quality first)
    strategies_sorted = sorted(enumerate(strategies), key=lambda x: x[1]['error'])
    
    for b in range(B):
        block_size = len(blocks[b])
        deadline = block_deadlines[b]
        
        assigned = False
        
        # Try slots in order
        for t in range(min(deadline + 1, delta)):
            if assigned:
                break
            
            # Try strategies (high quality first)
            for s_idx, strategy in strategies_sorted:
                # Check error constraint
                new_error = cumulative_error + strategy['error']
                avg_error = new_error / (b + 1)
                if avg_error > error_threshold:
                    continue
                
                # Check time capacity
                new_time_used = slot_time_used[t] + (strategy['duration'] * block_size / parallelism)
                if new_time_used > slot_time_capacity:
                    continue
                
                # Fits! Assign here
                new_load = slot_loads[t] + block_size
                emission_factor = get_emission_factor(new_load, capacity_tiers)
                cost = carbon[t] * strategy['duration'] * block_size * emission_factor
                
                slot_loads[t] += block_size
                slot_time_used[t] = new_time_used
                cumulative_error += strategy['error']
                total_cost += cost
                assignment.append((b, s_idx, t))
                
                assigned = True
                break
        
        if not assigned:
            # Fallback
            print(f"Warning: Block {b} has no feasible assignment", file=sys.stderr)
            s_idx = 0
            t = 0
            slot_loads[t] += block_size
            slot_time_used[t] += strategies[s_idx]['duration'] * block_size / parallelism
            cumulative_error += strategies[s_idx]['error']
            total_cost += carbon[t] * strategies[s_idx]['duration'] * block_size
            assignment.append((b, s_idx, t))
    
    avg_error = cumulative_error / B if B > 0 else 0
    
    return assignment, total_cost, avg_error, slot_loads, slot_time_used


def main():
    """Command line entry point"""
    
    if len(sys.argv) < 10:
        print("Usage: first_fit_time.py <requests.csv> <strategies.csv> <carbon.txt> "
              "<delta> <beta> <error> <slot_duration_min> <parallelism> [output.csv] [--capacity-file <file>]",
              file=sys.stderr)
        sys.exit(1)
    
    requests_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    delta = int(sys.argv[4])
    beta = int(sys.argv[5])
    error_threshold = float(sys.argv[6])
    slot_duration = int(sys.argv[7])
    parallelism = int(sys.argv[8])
    
    output_file = sys.argv[9] if len(sys.argv) > 9 and not sys.argv[9].startswith('--') else None
    
    capacity_tiers = None
    if '--capacity-file' in sys.argv:
        idx = sys.argv.index('--capacity-file')
        if idx + 1 < len(sys.argv):
            from greedy_time import load_capacity_tiers
            capacity_tiers = load_capacity_tiers(sys.argv[idx + 1])
            print(f"Loaded {len(capacity_tiers)} capacity tiers", file=sys.stderr)
    
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
    
    requests = [{'id': i, 'deadline': deadlines[i]} for i in range(len(deadlines))]
    block_size = len(requests) // beta
    blocks = [requests[i*block_size:(i+1)*block_size] for i in range(beta)]
    
    assignment, total_cost, avg_error, slot_loads, time_used = first_fit_time_aware(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration, parallelism, capacity_tiers
    )
    
    print(f"First-Fit Time-Aware solution:", file=sys.stderr)
    print(f"  Cost: {total_cost:.2f}", file=sys.stderr)
    print(f"  Error: {avg_error:.2f}%", file=sys.stderr)
    
    if output_file:
        with open(output_file, 'w') as f:
            f.write("request_id,strategy,slot\n")
            for block_idx, strategy_idx, slot in assignment:
                strategy_name = strategies[strategy_idx]['name']
                for req in blocks[block_idx]:
                    f.write(f"{req['id']},{strategy_name},{slot}\n")
            
            f.write(f"\nall_emissions: {total_cost}\n")
            f.write(f"all_errors: {avg_error}\n")
    
    print(f"COST: {total_cost}")
    print(f"ERROR: {avg_error}")
    print(f"MAX_LOAD: {max(slot_loads) if slot_loads else 0}")
    print(f"MAX_TIME: {max(time_used) if time_used else 0}")
    print(f"LOADS: {','.join(map(str, slot_loads))}")
    print(f"TIMES: {','.join(f'{t:.2f}' for t in time_used)}")


if __name__ == '__main__':
    main()
