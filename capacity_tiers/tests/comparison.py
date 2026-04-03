#!/usr/bin/env python3
"""
Capacity Tiers: Comprehensive Comparison Test

Compares three scheduling methods with capacity-aware emission factors:
1. Greedy (with lookahead) - fast, capacity-aware baseline
2. ProbabilisticSlack - online heuristic using deadline slack
3. DP with warm-start - optimal solution with pruning

Configuration uses:
- Generous deadlines to enable strategic planning
- Error budget constraints to expose differences
- Moderate problem size for reasonable execution time
"""

import sys
import os
import tempfile
import subprocess
import time
import random


# Test configuration - balanced for clear results
NUM_REQUESTS = 100
NUM_BLOCKS = 12  # ProbabilisticSlack works well with this
NUM_SLOTS = 10
ERROR_THRESHOLD = 3  # Tight to expose strategy differences
SEED = 42

# Capacity tiers with rebound effect
CAPACITY_TIERS = [
    (5, 1.0),    # 0-5: normal
    (10, 2.0),   # 6-10: 2x penalty
    (15, 4.0),   # 11-15: 4x penalty
    (30, 8.0),   # 16-30: 8x penalty (disaster!)
]

# Pruning configurations
KBEST_K = 5000   # Relaxed: keep more states
BEAM_K = 1000     # Relaxed: keep more states per error level 


def generate_inputs(temp_dir):
    """Generate test input files"""
    
    print(f"Generating test inputs...")
    print(f"  Requests: {NUM_REQUESTS}")
    print(f"  Blocks: {NUM_BLOCKS}")
    print(f"  Slots: {NUM_SLOTS}")
    print(f"  Error threshold: {ERROR_THRESHOLD}%")
    print()
    
    # Requests with GENEROUS deadlines (80-100% of slots)
    requests_file = os.path.join(temp_dir, 'requests.csv')
    random.seed(SEED)
    min_deadline = int(NUM_SLOTS * 0.8)  # Can use last 80% of slots
    deadlines = [random.randint(min_deadline, NUM_SLOTS-1) for _ in range(NUM_REQUESTS)]
    
    with open(requests_file, 'w') as f:
        f.write(','.join(map(str, deadlines)))
    
    print(f"Deadlines: min={min(deadlines)}, max={max(deadlines)}, slots={NUM_SLOTS}")
    print(f"  → Requests can use last {100*(NUM_SLOTS-min(deadlines))/NUM_SLOTS:.0f}% of slots")
    print()
    
    # Strategies with error/duration trade-offs
    strategies_file = os.path.join(temp_dir, 'strategies.csv')
    with open(strategies_file, 'w') as f:
        f.write('error,duration,name\n')
        f.write('0,120,High\n')    # No error, long duration
        f.write('2,60,Medium\n')   # Some error, medium duration
        f.write('5,30,Low\n')      # High error, short duration
    
    # Carbon intensity: decreasing pattern (later is greener)
    carbon_file = os.path.join(temp_dir, 'carbon.txt')
    carbon_pattern = [
        200, 180, 160, 140,  # Early slots: high carbon
        120, 110, 100, 90,   # Middle slots: medium carbon
        80, 70, 60, 50       # Late slots: low carbon (green!)
    ]
    
    with open(carbon_file, 'w') as f:
        for c in carbon_pattern:
            f.write(f'{c}\n')
    
    print(f"Carbon pattern: {min(carbon_pattern)} (green) to {max(carbon_pattern)} (brown) gCO2/kWh")
    print(f"  → Later slots are greener (rewards strategic planning)")
    print()
    
    # Capacity tiers
    tiers_file = os.path.join(temp_dir, 'tiers.csv')
    with open(tiers_file, 'w') as f:
        for capacity, factor in CAPACITY_TIERS:
            f.write(f'{capacity},{factor}\n')
    
    print(f"Capacity tiers: {len(CAPACITY_TIERS)} levels")
    for cap, fac in CAPACITY_TIERS:
        print(f"  {cap:3d} requests → {fac:.1f}x emission factor")
    print()
    
    return requests_file, strategies_file, carbon_file, tiers_file


def run_command(label, cmd, timeout=400):
    """Execute command and parse results"""
    
    print(f"{'='*80}")
    print(f"{label}")
    print(f"{'='*80}")
    
    start = time.time()
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        elapsed = time.time() - start
        
        if result.returncode != 0:
            print(f"❌ FAILED (exit code {result.returncode})")
            if result.stderr:
                # Print first few lines of stderr
                stderr_lines = result.stderr.split('\n')[:10]
                print(f"Error: {chr(10).join(stderr_lines)}")
            return None
        
        # Parse output
        cost = None
        error = None
        loads = None
        states = None
        
        for line in result.stdout.split('\n'):
            if line.startswith('COST:'):
                cost = float(line.split(':')[1].strip())
            elif line.startswith('ERROR:'):
                error = float(line.split(':')[1].strip())
            elif line.startswith('LOADS:'):
                loads = list(map(int, line.split(':')[1].strip().split(',')))
        
        # Parse stats from stderr (DP only)
        for line in result.stderr.split('\n'):
            if 'States explored:' in line:
                states = int(line.split(':')[1].strip().replace(',', ''))
        
        print(f"✅ SUCCESS")
        print(f"  Cost: {cost:,.0f}")
        print(f"  Error: {error:.2f}%")
        print(f"  Time: {elapsed:.2f}s")
        if states:
            print(f"  States: {states:,}")
        print()
        
        return {
            'cost': cost,
            'error': error,
            'loads': loads,
            'time': elapsed,
            'states': states
        }
        
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        print(f"⏱️ TIMEOUT after {elapsed:.0f}s\n")
        return None


def print_comparison(results):
    """Print comparison table and analysis"""
    
    print(f"\n{'='*80}")
    print("COMPARISON RESULTS")
    print(f"{'='*80}\n")
    
    # Find optimal cost
    costs = [r['cost'] for r in results.values() if r and r['cost']]
    if not costs:
        print("No valid results to compare")
        return
    
    optimal_cost = min(costs)
    
    # Table header
    print(f"{'Method':<30} {'Cost':>12} {'Gap':>8} {'Error':>8} {'Time':>8} {'States':>12}")
    print(f"{'-'*30} {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")
    
    # Table rows
    for label, result in results.items():
        if result is None:
            print(f"{label:<30} {'FAILED':>12}")
            continue
        
        cost = result['cost']
        gap = ((cost - optimal_cost) / optimal_cost * 100) if cost > 0 else 0
        error = result['error']
        time_val = result['time']
        states = result.get('states')
        
        marker = "🏆" if cost == optimal_cost else "  "
        states_str = f"{states:,}" if states else "-"
        
        print(f"{marker}{label:<28} {cost:>12,.0f} {gap:>7.1f}% {error:>7.2f}% {time_val:>7.2f}s {states_str:>12}")
    
    print(f"\n🏆 = Optimal solution (lowest cost)")
    
    # Analysis
    print(f"\n{'='*80}")
    print("ANALYSIS")
    print(f"{'='*80}\n")
    
    greedy_res = results.get('Greedy')
    prob_res = results.get('ProbabilisticSlack')
    dp_nowarm_res = results.get('DP (no warm-start)')
    dp_warm_res = results.get('DP + Warm-Start')
    
    if greedy_res and dp_nowarm_res:
        if dp_nowarm_res['cost'] < greedy_res['cost']:
            improvement = ((greedy_res['cost'] - dp_nowarm_res['cost']) / greedy_res['cost'] * 100)
            print(f"✅ DP found better solution than Greedy!")
            print(f"   Improvement: {improvement:.1f}%")
            print(f"   Greedy: {greedy_res['cost']:,.0f}")
            print(f"   DP:     {dp_nowarm_res['cost']:,.0f}")
        else:
            print(f"✓ Greedy matched DP optimal")
            print(f"   Both found: {greedy_res['cost']:,.0f}")
        print()
    
    if dp_nowarm_res and dp_warm_res:
        states_before = dp_nowarm_res.get('states', 0)
        states_after = dp_warm_res.get('states', 0)
        if states_before > 0:
            reduction = ((states_before - states_after) / states_before * 100)
            speedup = dp_nowarm_res['time'] / dp_warm_res['time'] if dp_warm_res['time'] > 0 else 0
            
            print(f"⚡ Warm-start efficiency:")
            print(f"   States: {states_before:,} → {states_after:,} ({reduction:.1f}% reduction)")
            print(f"   Time: {dp_nowarm_res['time']:.2f}s → {dp_warm_res['time']:.2f}s ({speedup:.1f}x speedup)")
            
            if dp_warm_res['cost'] == dp_nowarm_res['cost']:
                print(f"   ✓ Maintained optimality")
            else:
                gap = ((dp_warm_res['cost'] - dp_nowarm_res['cost']) / dp_nowarm_res['cost'] * 100)
                print(f"   ⚠ Cost gap: {gap:.2f}%")
        print()
    
    # Load distribution comparison
    print(f"{'='*80}")
    print("LOAD DISTRIBUTION")
    print(f"{'='*80}\n")
    
    for label, result in results.items():
        if result and result.get('loads'):
            loads = result['loads']
            non_zero = [(i, l) for i, l in enumerate(loads) if l > 0]
            max_load = max(loads) if loads else 0
            total_load = sum(loads)
            
            print(f"{label}:")
            print(f"  Slots used: {len(non_zero)}/{len(loads)}")
            print(f"  Max load: {max_load} requests")
            
            # Show distribution
            if non_zero:
                print(f"  Distribution: ", end="")
                for slot, load in non_zero[:8]:  # First 8 slots
                    print(f"[{slot}:{load}]", end=" ")
                if len(non_zero) > 8:
                    print(f"+ {len(non_zero)-8} more", end="")
                print()
            print()


def main():
    """Main test execution"""
    
    print(f"{'='*80}")
    print("CAPACITY TIERS: COMPREHENSIVE COMPARISON")
    print(f"{'='*80}\n")
    
    # Get script paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    
    greedy_script = os.path.join(parent_dir, 'greedy.py')
    prob_slack_script = os.path.join(parent_dir, 'probabilistic_slack.py')
    dp_script = os.path.join(parent_dir, 'dp_warmstart.py')
    
    with tempfile.TemporaryDirectory() as temp_dir:
        
        # Generate inputs
        req_file, strat_file, carbon_file, tiers_file = generate_inputs(temp_dir)
        
        results = {}
        
        # 1. Greedy (baseline)
        greedy_out = os.path.join(temp_dir, 'greedy.csv')
        cmd = [
            sys.executable, greedy_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            greedy_out,
            '--capacity-file', tiers_file
        ]
        
        results['Greedy'] = run_command("1. GREEDY (with lookahead)", cmd)
        
        if results['Greedy'] is None:
            print("⚠️ Greedy failed - cannot continue with warm-start")
            return
        
        greedy_cost = results['Greedy']['cost']
        
        # 2. ProbabilisticSlack
        prob_out = os.path.join(temp_dir, 'prob.csv')
        cmd = [
            sys.executable, prob_slack_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            prob_out,
            '--capacity-file', tiers_file
        ]
        
        results['ProbabilisticSlack'] = run_command("2. PROBABILISTIC SLACK", cmd)
        
        # 3. DP without warm-start
        dp_out1 = os.path.join(temp_dir, 'dp_nowarm.csv')
        cmd = [
            sys.executable, dp_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            dp_out1,
            '--capacity-file', tiers_file
        ]
        
        results['DP (no warm-start)'] = run_command("3. DP (NO WARM-START)", cmd)
        
        # 4. DP with warm-start
        dp_out2 = os.path.join(temp_dir, 'dp_warm.csv')
        cmd = [
            sys.executable, dp_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            dp_out2,
            '--capacity-file', tiers_file,
            '--upper-bound', str(greedy_cost)
        ]
        
        results['DP + Warm-Start'] = run_command("4. DP + WARM-START", cmd)
        
        # 5. DP with K-Best pruning (relaxed)
        dp_out3 = os.path.join(temp_dir, 'dp_kbest.csv')
        cmd = [
            sys.executable, dp_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            dp_out3,
            '--capacity-file', tiers_file,
            '--upper-bound', str(greedy_cost),
            '--pruning', 'kbest',
            '--pruning-k', str(KBEST_K)
        ]
        
        results['DP + K-Best'] = run_command("5. DP + K-BEST PRUNING (relaxed)", cmd)
        
        # 6. DP with Beam Search (relaxed)
        dp_out4 = os.path.join(temp_dir, 'dp_beam.csv')
        cmd = [
            sys.executable, dp_script,
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            dp_out4,
            '--capacity-file', tiers_file,
            '--upper-bound', str(greedy_cost),
            '--pruning', 'beam',
            '--pruning-k', str(BEAM_K)
        ]
        
        results['DP + Beam'] = run_command("6. DP + BEAM SEARCH (relaxed)", cmd)
        
        # Comparison
        print_comparison(results)
    
    print(f"\n✅ Test completed!\n")


if __name__ == '__main__':
    main()
