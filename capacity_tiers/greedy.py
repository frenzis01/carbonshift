#!/usr/bin/env python3
"""
Greedy Scheduler con Capacity Tiers Awareness

Euristica greedy che assegna ogni blocco minimizzando il costo totale
considerando i capacity tiers. Usato come warm-start per la DP.
"""

import sys
import csv


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
    
    # Ordina per capacità crescente
    tiers.sort(key=lambda x: x['capacity'])
    return tiers


def get_emission_factor(load, tiers):
    """Calcola emission factor basato sul carico e i tiers"""
    if not tiers:
        return 1.0
    
    for tier in tiers:
        if load <= tier['capacity']:
            return tier['factor']
    
    # Se supera tutti i tier, usa l'ultimo
    return tiers[-1]['factor']


def greedy_capacity_aware(blocks, strategies, carbon, delta, error_threshold, capacity_tiers=None):
    """
    Assegnazione greedy capacity-aware.
    
    Per ogni blocco, prova tutte le combinazioni (strategy, slot) feasibili
    e sceglie quella con minimo costo considerando:
    - Carbon intensity dello slot
    - Emission factor basato sul load corrente dello slot
    - Vincolo di deadline
    - Vincolo di errore cumulativo
    
    Args:
        blocks: Lista di blocchi [[req1, req2, ...], ...]
        strategies: Lista di dict {'error': ..., 'duration': ...}
        carbon: Lista di carbon intensity per slot
        delta: Numero di time slots
        error_threshold: Soglia di errore per richiesta (%)
        capacity_tiers: Lista di tier (opzionale)
    
    Returns:
        assignment: Lista di (block_idx, strategy_idx, slot)
        total_cost: Costo totale
        total_error: Errore medio totale
    """
    
    if capacity_tiers is None:
        capacity_tiers = [{'capacity': 999999, 'factor': 1.0}]
    
    B = len(blocks)
    
    # Tracking
    slot_loads = [0] * delta  # Quante richieste per slot
    cumulative_error = 0
    total_cost = 0.0
    assignment = []
    
    # Calcolo deadline per blocco (min delle deadline interne)
    block_deadlines = [min(req["deadline"] for req in group) for group in blocks]
    
    # Processa blocchi in ordine
    for b in range(B):
        block_size = len(blocks[b])
        deadline = block_deadlines[b]
        
        best_cost = float('inf')
        best_choice = None
        
        # Prova tutte le combinazioni feasibili
        for s_idx, strategy in enumerate(strategies):
            strategy_error = strategy['error']
            strategy_duration = strategy['duration']
            
            # Vincolo errore globale
            new_cumulative_error = cumulative_error + strategy_error
            if new_cumulative_error > error_threshold * B:
                continue
            
            for t in range(delta):
                # Vincolo deadline
                if t > deadline:
                    continue
                
                # Calcola costo con capacity tier
                new_load = slot_loads[t] + block_size
                emission_factor = get_emission_factor(new_load, capacity_tiers)
                
                # Formula: carbon * duration * block_size * emission_factor
                cost = carbon[t] * strategy_duration * block_size * emission_factor
                
                if cost < best_cost:
                    best_cost = cost
                    best_choice = (s_idx, t)
        
        # Se non trovato feasible, fallback a strategia più veloce nello slot più green
        if best_choice is None:
            # Prova senza vincolo errore
            min_carbon_slot = min(range(min(deadline+1, delta)), key=lambda t: carbon[t])
            fastest_strategy = min(range(len(strategies)), key=lambda s: strategies[s]['duration'])
            best_choice = (fastest_strategy, min_carbon_slot)
            
            s_idx, t = best_choice
            new_load = slot_loads[t] + block_size
            emission_factor = get_emission_factor(new_load, capacity_tiers)
            best_cost = carbon[t] * strategies[s_idx]['duration'] * block_size * emission_factor
        
        # Applica scelta
        s_idx, t = best_choice
        slot_loads[t] += block_size
        cumulative_error += strategies[s_idx]['error']
        total_cost += best_cost
        assignment.append((b, s_idx, t))
    
    # Calcola errore medio
    total_error = cumulative_error / B if B > 0 else 0
    
    return assignment, total_cost, total_error, slot_loads


def main():
    """Entry point da command line"""
    
    if len(sys.argv) < 7:
        print("Usage: greedy_ct.py <requests.csv> <strategies.csv> <carbon.txt> "
              "<delta> <beta> <error> [output.csv] [--capacity-file <file>]",
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
    
    # Check for capacity file
    capacity_tiers = None
    if '--capacity-file' in sys.argv:
        idx = sys.argv.index('--capacity-file')
        if idx + 1 < len(sys.argv):
            capacity_file = sys.argv[idx + 1]
            capacity_tiers = load_capacity_tiers(capacity_file)
            print(f"Loaded {len(capacity_tiers)} capacity tiers from {capacity_file}", file=sys.stderr)
    
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
    
    # Crea blocchi
    requests = [{'id': i, 'deadline': deadlines[i]} for i in range(len(deadlines))]
    block_size = len(requests) // beta
    blocks = [requests[i*block_size:(i+1)*block_size] for i in range(beta)]
    
    # Esegui greedy
    assignment, total_cost, total_error, slot_loads = greedy_capacity_aware(
        blocks, strategies, carbon, delta, error_threshold, capacity_tiers
    )
    
    # Output
    print(f"Greedy solution found:", file=sys.stderr)
    print(f"  Total cost: {total_cost:.2f}", file=sys.stderr)
    print(f"  Average error: {total_error:.2f}%", file=sys.stderr)
    print(f"  Slot loads: {slot_loads}", file=sys.stderr)
    print(f"  Max load: {max(slot_loads)}", file=sys.stderr)
    
    # Write output if specified
    if output_file:
        with open(output_file, 'w') as f:
            f.write("request_id,strategy,slot\n")
            for block_idx, strategy_idx, slot in assignment:
                strategy_name = strategies[strategy_idx]['name']
                for req in blocks[block_idx]:
                    f.write(f"{req['id']},{strategy_name},{slot}\n")
            
            f.write(f"\nall_emissions: {total_cost}\n")
            f.write(f"all_errors: {total_error}\n")
        
        print(f"Written to {output_file}", file=sys.stderr)
    
    # Print metrics to stdout (for parsing)
    print(f"COST: {total_cost}")
    print(f"ERROR: {total_error}")
    print(f"MAX_LOAD: {max(slot_loads)}")
    print(f"LOADS: {','.join(map(str, slot_loads))}")


if __name__ == '__main__':
    main()
