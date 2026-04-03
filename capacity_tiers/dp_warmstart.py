#!/usr/bin/env python3
"""
Dynamic Programming con Capacity Tiers, Warm-Start e Pruning

Versione ottimizzata che:
1. Accetta una soluzione iniziale come upper bound (warm-start)
2. Supporta K-Best Pruning (mantiene solo top-K stati globali)
3. Supporta Beam Search (mantiene top-K stati per livello di errore)
4. Pota percorsi che superano l'upper bound
"""

import sys
import csv
from collections import defaultdict

SCALE_FACTOR = 100  # Per gestire errori come interi


def load_capacity_tiers(filepath):
    """Carica capacity tiers da file CSV"""
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
    """Calcola emission factor basato sul carico"""
    if not tiers:
        return 1.0
    
    for tier in tiers:
        if load <= tier['capacity']:
            return tier['factor']
    
    return tiers[-1]['factor']


def solve_with_dp_warmstart(blocks, strategies, carbon, delta, error_threshold, 
                             capacity_tiers=None, upper_bound=None, 
                             pruning_mode=None, pruning_k=None):
    """
    DP con warm-start e pruning.
    
    Args:
        blocks: Lista di blocchi
        strategies: Lista di strategie
        carbon: Carbon intensity per slot
        delta: Numero di time slots
        error_threshold: Soglia errore per richiesta
        capacity_tiers: Tier di capacità (opzionale)
        upper_bound: Costo massimo accettabile (warm-start)
        pruning_mode: None, 'kbest', o 'beam'
        pruning_k: Numero di stati da mantenere
    
    Returns:
        assignment, cost, error, stats
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    B = len(blocks)
    S = range(len(strategies))
    T = range(delta)
    
    # Deadline per blocco
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # Massimo errore cumulativo
    E_max = error_threshold * B * SCALE_FACTOR
    
    # Stato iniziale: (errore=0, loads=(0,0,...,0))
    initial_loads = tuple([0] * delta)
    D_prev = {(0, initial_loads): 0}
    trace = {}
    
    # Statistiche
    stats = {
        'states_explored': 0,
        'states_pruned_bound': 0,
        'states_pruned_kbest': 0,
        'max_states_per_block': 0
    }
    
    # DP loop
    for b in range(1, B + 1):
        D_curr = {}
        block_index = b - 1
        block_size = len(blocks[block_index])
        
        # Itera su tutti gli stati precedenti
        for (e_prev, loads_prev), cost_prev in D_prev.items():
            
            # PRUNING: Se già superiamo upper bound, scarta
            if upper_bound is not None and cost_prev > upper_bound:
                stats['states_pruned_bound'] += 1
                continue
            
            # Prova tutte le assegnazioni possibili
            for s in S:
                for t in T:
                    
                    # Vincolo deadline
                    if t > block_deadlines[block_index]:
                        continue
                    
                    # Calcola nuovo stato
                    loads_list = list(loads_prev)
                    new_load_t = loads_list[t] + block_size
                    loads_list[t] = new_load_t
                    loads_current = tuple(loads_list)
                    
                    # Emission factor basato sul nuovo load
                    emission_factor = get_emission_factor(new_load_t, capacity_tiers)
                    
                    # Calcola costo
                    error_s = strategies[s]["error"] * SCALE_FACTOR
                    carbon_cost = carbon[t] * strategies[s]["duration"] * block_size * emission_factor
                    e_current = e_prev + error_s
                    
                    # Vincolo errore
                    if e_current > E_max:
                        continue
                    
                    new_cost = cost_prev + carbon_cost
                    
                    # PRUNING: Skip if STRICTLY greater than upper bound
                    # Use > not >= to explore ALL solutions with cost <= upper_bound
                    if upper_bound is not None and new_cost > upper_bound:
                        stats['states_pruned_bound'] += 1
                        continue
                    
                    # Aggiorna stato
                    state_key = (e_current, loads_current)
                    
                    if state_key not in D_curr or new_cost < D_curr[state_key]:
                        D_curr[state_key] = new_cost
                        trace[state_key] = ((e_prev, loads_prev), s, t)
                        stats['states_explored'] += 1
        
        # PRUNING: Applica strategia di pruning se specificata
        if pruning_mode and pruning_k and len(D_curr) > pruning_k:
            D_curr = apply_pruning(D_curr, pruning_mode, pruning_k, stats)
        
        # SAFETY CHECK: Se pruning ha eliminato tutti gli stati, fallback
        if not D_curr:
            print(f"  WARNING: Pruning eliminated all states at block {b}!", file=sys.stderr)
            print(f"           This means no feasible solution exists with current constraints", file=sys.stderr)
            # Ritorna infeasible
            return None, float('inf'), -1, None, stats
        
        D_prev = D_curr
        stats['max_states_per_block'] = max(stats['max_states_per_block'], len(D_curr))
        
        # Progress
        if b % max(1, B // 10) == 0:
            print(f"  Block {b}/{B}: {len(D_curr)} states", file=sys.stderr)
    
    # Trova soluzione finale
    if not D_prev:
        return None, float('inf'), -1, None, stats
    
    min_cost = min(D_prev.values())
    final_state = min(D_prev.items(), key=lambda x: x[1])[0]
    
    # Ricostruisci assegnazione
    assignment = reconstruct_assignment(final_state, trace, B)
    
    # Calcola errore medio e estrai loads finali
    final_error = final_state[0] / SCALE_FACTOR / B if B > 0 else 0
    final_loads = list(final_state[1])  # Converti tuple a list
    
    return assignment, min_cost, final_error, final_loads, stats


def apply_pruning(D_curr, mode, k, stats):
    """Applica strategia di pruning"""
    
    if mode == 'kbest':
        # K-Best: Tieni solo i top-K stati globalmente
        sorted_states = sorted(D_curr.items(), key=lambda x: x[1])
        pruned = dict(sorted_states[:k])
        stats['states_pruned_kbest'] += len(D_curr) - len(pruned)
        return pruned
    
    elif mode == 'beam':
        # Beam Search: Tieni top-K per ogni livello di errore
        by_error = defaultdict(list)
        for (e, loads), cost in D_curr.items():
            by_error[e].append(((e, loads), cost))
        
        pruned = {}
        for e, states in by_error.items():
            # Ordina per costo e prendi top-k
            sorted_states = sorted(states, key=lambda x: x[1])
            for state_key, cost in sorted_states[:k]:
                pruned[state_key] = cost
        
        stats['states_pruned_kbest'] += len(D_curr) - len(pruned)
        return pruned
    
    return D_curr


def reconstruct_assignment(final_state, trace, B):
    """Ricostruisci assegnazione da trace"""
    assignment = []
    current_state = final_state
    
    for b in range(B, 0, -1):
        if current_state not in trace:
            break
        
        prev_state, strategy, slot = trace[current_state]
        assignment.append((b-1, strategy, slot))
        current_state = prev_state
    
    assignment.reverse()
    return assignment


def main():
    """Entry point"""
    
    if len(sys.argv) < 7:
        print("Usage: carbonshiftDP_ct1.py <requests> <strategies> <carbon> "
              "<delta> <beta> <error> [output] [options]", file=sys.stderr)
        print("\nOptions:", file=sys.stderr)
        print("  --capacity-file <file>     Capacity tiers CSV", file=sys.stderr)
        print("  --upper-bound <cost>       Initial upper bound (warm-start)", file=sys.stderr)
        print("  --pruning <mode>           kbest or beam", file=sys.stderr)
        print("  --pruning-k <K>            Number of states to keep", file=sys.stderr)
        sys.exit(1)
    
    # Parse arguments
    requests_file = sys.argv[1]
    strategies_file = sys.argv[2]
    carbon_file = sys.argv[3]
    delta = int(sys.argv[4])
    beta = int(sys.argv[5])
    error_threshold = float(sys.argv[6])
    
    output_file = sys.argv[7] if len(sys.argv) > 7 and not sys.argv[7].startswith('--') else None
    
    # Parse options
    capacity_tiers = None
    upper_bound = None
    pruning_mode = None
    pruning_k = None
    
    i = 7 if output_file else 6
    while i < len(sys.argv):
        if sys.argv[i] == '--capacity-file' and i+1 < len(sys.argv):
            capacity_tiers = load_capacity_tiers(sys.argv[i+1])
            print(f"Loaded {len(capacity_tiers)} capacity tiers", file=sys.stderr)
            i += 2
        elif sys.argv[i] == '--upper-bound' and i+1 < len(sys.argv):
            upper_bound = float(sys.argv[i+1])
            print(f"Using upper bound: {upper_bound}", file=sys.stderr)
            i += 2
        elif sys.argv[i] == '--pruning' and i+1 < len(sys.argv):
            pruning_mode = sys.argv[i+1]
            print(f"Pruning mode: {pruning_mode}", file=sys.stderr)
            i += 2
        elif sys.argv[i] == '--pruning-k' and i+1 < len(sys.argv):
            pruning_k = int(sys.argv[i+1])
            print(f"Pruning K: {pruning_k}", file=sys.stderr)
            i += 2
        else:
            i += 1
    
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
    
    # Solve
    print("Solving with DP...", file=sys.stderr)
    assignment, cost, error, final_loads, stats = solve_with_dp_warmstart(
        blocks, strategies, carbon, delta, error_threshold,
        capacity_tiers, upper_bound, pruning_mode, pruning_k
    )
    
    # Output results
    print(f"\nSolution found:", file=sys.stderr)
    print(f"  Cost: {cost:.2f}", file=sys.stderr)
    print(f"  Error: {error:.2f}%", file=sys.stderr)
    if final_loads:
        print(f"  Final loads: {final_loads}", file=sys.stderr)
        print(f"  Max load: {max(final_loads)}", file=sys.stderr)
    print(f"\nStatistics:", file=sys.stderr)
    print(f"  States explored: {stats['states_explored']}", file=sys.stderr)
    print(f"  States pruned (bound): {stats['states_pruned_bound']}", file=sys.stderr)
    print(f"  States pruned (k-best): {stats['states_pruned_kbest']}", file=sys.stderr)
    print(f"  Max states per block: {stats['max_states_per_block']}", file=sys.stderr)
    
    # Write output
    if output_file and assignment:
        with open(output_file, 'w') as f:
            f.write("request_id,strategy,slot\n")
            for block_idx, strategy_idx, slot in assignment:
                strategy_name = strategies[strategy_idx]['name']
                for req in blocks[block_idx]:
                    f.write(f"{req['id']},{strategy_name},{slot}\n")
            
            f.write(f"\nall_emissions: {cost}\n")
            f.write(f"all_errors: {error}\n")
        
        print(f"Written to {output_file}", file=sys.stderr)
    
    # Print to stdout
    print(f"COST: {cost}")
    print(f"ERROR: {error}")
    if final_loads:
        print(f"MAX_LOAD: {max(final_loads)}")
        print(f"LOADS: {','.join(map(str, final_loads))}")


if __name__ == '__main__':
    main()
