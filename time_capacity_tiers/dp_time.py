#!/usr/bin/env python3
"""
DP Time-Aware Scheduler with Warm-Start and Pruning

Dynamic Programming solution for time-capacity-aware scheduling.

State: D[block][error][loads_tuple][times_tuple] = min_cost

Where:
- error: Cumulative error so far
- loads_tuple: Tuple of request counts per slot
- times_tuple: Tuple of time used per slot

Pruning strategies:
1. Warm-start: Use greedy solution as upper bound
2. K-Best: Keep top-K states globally
3. Beam Search: Keep top-K states per error level
"""

import sys
import csv
import time as time_module
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


def dp_time_aware(blocks, strategies, carbon, delta, error_threshold,
                  slot_duration_minutes, parallelism,
                  capacity_tiers=None,
                  upper_bound=None,
                  pruning_type=None,
                  pruning_k=None):
    """
    DP time-aware scheduler with pruning.
    
    Args:
        blocks: List of request blocks
        strategies: List of strategy dicts
        carbon: Carbon intensity per slot
        delta: Number of time slots
        error_threshold: Max average error
        slot_duration_minutes: Duration of each slot in minutes
        parallelism: Number of parallel executions per slot
        capacity_tiers: Capacity tier configuration
        upper_bound: Initial upper bound for warm-start pruning
        pruning_type: 'kbest' or 'beam' or None
        pruning_k: Number of states to keep (for pruning)
    
    Returns:
        assignment, cost, error, loads, times, stats
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    start_time = time_module.time()
    
    B = len(blocks)
    E_max = int(error_threshold * B)  # Max cumulative error
    slot_time_capacity = slot_duration_minutes * parallelism
    
    block_sizes = [len(block) for block in blocks]
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # DP state: D[block][error, loads, times] = cost
    # Use dict for sparse storage
    D = [defaultdict(lambda: float('inf')) for _ in range(B + 1)]
    trace = [dict() for _ in range(B + 1)]
    
    # Initial state: no blocks assigned, zero error, empty slots
    initial_loads = tuple([0] * delta)
    initial_times = tuple([0.0] * delta)
    D[0][(0, initial_loads, initial_times)] = 0
    
    stats = {
        'states_explored': 0,
        'states_pruned_bound': 0,
        'states_pruned_time': 0,
        'states_pruned_k': 0
    }
    
    # DP iteration
    for b in range(B):
        block_size = block_sizes[b]
        deadline = block_deadlines[b]
        
        # Apply pruning if specified
        if pruning_type and pruning_k and len(D[b]) > pruning_k:
            if pruning_type == 'kbest':
                # Keep top-K by cost globally
                sorted_states = sorted(D[b].items(), key=lambda x: x[1])
                new_states = dict(sorted_states[:pruning_k])
                stats['states_pruned_k'] += len(D[b]) - len(new_states)
                D[b] = defaultdict(lambda: float('inf'), new_states)
            
            elif pruning_type == 'beam':
                # Keep top-K per error level
                states_by_error = defaultdict(list)
                for state_key, cost in D[b].items():
                    e_prev = state_key[0]
                    states_by_error[e_prev].append((state_key, cost))
                
                new_states = {}
                for e_prev, state_list in states_by_error.items():
                    sorted_states = sorted(state_list, key=lambda x: x[1])
                    for state_key, cost in sorted_states[:pruning_k]:
                        new_states[state_key] = cost
                
                stats['states_pruned_k'] += len(D[b]) - len(new_states)
                D[b] = defaultdict(lambda: float('inf'), new_states)
        
        D_curr = D[b + 1]
        
        # Process each state
        for state_key, cost_prev in D[b].items():
            e_prev, loads_prev, times_prev = state_key
            
            # Try all (strategy, slot) combinations
            for s, strategy in enumerate(strategies):
                error_s = strategy['error']
                duration_s = strategy['duration']
                e_current = e_prev + error_s
                
                # Error constraint
                if e_current > E_max:
                    continue
                
                for t in range(min(deadline + 1, delta)):
                    stats['states_explored'] += 1
                    
                    # Update loads and times
                    loads_list = list(loads_prev)
                    times_list = list(times_prev)
                    
                    new_load = loads_list[t] + block_size
                    loads_list[t] = new_load
                    
                    time_needed = duration_s * block_size / parallelism
                    new_time = times_list[t] + time_needed
                    times_list[t] = new_time
                    
                    # Time capacity constraint
                    if new_time > slot_time_capacity:
                        stats['states_pruned_time'] += 1
                        continue
                    
                    loads_current = tuple(loads_list)
                    times_current = tuple(times_list)
                    
                    # Calculate cost
                    emission_factor = get_emission_factor(new_load, capacity_tiers)
                    carbon_cost = carbon[t] * duration_s * block_size * emission_factor
                    new_cost = cost_prev + carbon_cost
                    
                    # Warm-start pruning: skip if > upper bound
                    if upper_bound is not None and new_cost > upper_bound:
                        stats['states_pruned_bound'] += 1
                        continue
                    
                    # Update state
                    state_key_new = (e_current, loads_current, times_current)
                    
                    if new_cost < D_curr[state_key_new]:
                        D_curr[state_key_new] = new_cost
                        trace[b + 1][state_key_new] = (state_key, s, t)
    
    # Find optimal final state
    if not D[B]:
        # No feasible solution
        return None, float('inf'), None, None, None, stats
    
    final_state = min(D[B].items(), key=lambda x: x[1])
    final_state_key, final_cost = final_state
    final_error, final_loads, final_times = final_state_key
    
    # Reconstruct solution
    assignment = []
    current_state = final_state_key
    
    for b in range(B, 0, -1):
        if current_state not in trace[b]:
            break
        
        prev_state, strategy_idx, slot = trace[b][current_state]
        assignment.append((b - 1, strategy_idx, slot))
        current_state = prev_state
    
    assignment.reverse()
    
    avg_error = final_error / B if B > 0 else 0
    
    elapsed = time_module.time() - start_time
    stats['time_seconds'] = elapsed
    stats['final_states'] = len(D[B])
    
    return assignment, final_cost, avg_error, list(final_loads), list(final_times), stats


def main():
    """Command line entry point"""
    
    if len(sys.argv) < 10:
        print("Usage: dp_time.py <requests> <strategies> <carbon> <delta> <beta> "
              "<error> <slot_duration_min> <parallelism> [output] [options]",
              file=sys.stderr)
        print("\nOptions:", file=sys.stderr)
        print("  --capacity-file <file>     Capacity tiers CSV", file=sys.stderr)
        print("  --upper-bound <cost>       Initial upper bound (warm-start)", file=sys.stderr)
        print("  --pruning <type>           Pruning type: 'kbest' or 'beam'", file=sys.stderr)
        print("  --pruning-k <K>            Number of states to keep", file=sys.stderr)
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
    
    # Parse options
    capacity_tiers = None
    upper_bound = None
    pruning_type = None
    pruning_k = None
    
    if '--capacity-file' in sys.argv:
        idx = sys.argv.index('--capacity-file')
        if idx + 1 < len(sys.argv):
            capacity_tiers = load_capacity_tiers(sys.argv[idx + 1])
            print(f"Loaded {len(capacity_tiers)} capacity tiers", file=sys.stderr)
    
    if '--upper-bound' in sys.argv:
        idx = sys.argv.index('--upper-bound')
        if idx + 1 < len(sys.argv):
            upper_bound = float(sys.argv[idx + 1])
            print(f"Using upper bound: {upper_bound}", file=sys.stderr)
    
    if '--pruning' in sys.argv:
        idx = sys.argv.index('--pruning')
        if idx + 1 < len(sys.argv):
            pruning_type = sys.argv[idx + 1]
            print(f"Using {pruning_type} pruning", file=sys.stderr)
    
    if '--pruning-k' in sys.argv:
        idx = sys.argv.index('--pruning-k')
        if idx + 1 < len(sys.argv):
            pruning_k = int(sys.argv[idx + 1])
            print(f"Keeping top {pruning_k} states", file=sys.stderr)
    
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
    
    requests = [{'id': i, 'deadline': deadlines[i]} for i in range(len(deadlines))]
    block_size = len(requests) // beta
    blocks = [requests[i*block_size:(i+1)*block_size] for i in range(beta)]
    
    # Run DP
    print(f"Running DP time-aware...", file=sys.stderr)
    assignment, total_cost, avg_error, loads, times, stats = dp_time_aware(
        blocks, strategies, carbon, delta, error_threshold,
        slot_duration, parallelism, capacity_tiers,
        upper_bound, pruning_type, pruning_k
    )
    
    if assignment is None:
        print("No feasible solution found!", file=sys.stderr)
        sys.exit(1)
    
    # Output statistics
    print(f"\nDP Time-Aware solution:", file=sys.stderr)
    print(f"  Cost: {total_cost:.2f}", file=sys.stderr)
    print(f"  Error: {avg_error:.2f}%", file=sys.stderr)
    print(f"  Time: {stats['time_seconds']:.2f}s", file=sys.stderr)
    print(f"  States explored: {stats['states_explored']:,}", file=sys.stderr)
    print(f"  States pruned (bound): {stats['states_pruned_bound']:,}", file=sys.stderr)
    print(f"  States pruned (time): {stats['states_pruned_time']:,}", file=sys.stderr)
    print(f"  States pruned (k-pruning): {stats['states_pruned_k']:,}", file=sys.stderr)
    print(f"  Final states: {stats['final_states']:,}", file=sys.stderr)
    
    # Write output
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
    print(f"STATES: {stats['states_explored']}")
    print(f"MAX_LOAD: {max(loads) if loads else 0}")
    print(f"MAX_TIME: {max(times) if times else 0}")
    print(f"LOADS: {','.join(map(str, loads))}")
    print(f"TIMES: {','.join(f'{t:.2f}' for t in times)}")


if __name__ == '__main__':
    main()
