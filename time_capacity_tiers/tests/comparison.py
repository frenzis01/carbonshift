#!/usr/bin/env python3
"""
Time-Capacity Tiers: Comprehensive Comparison Test

Compares schedulers with time and capacity constraints:
1. Greedy Time-Aware - fast baseline with lookahead
2. First-Fit Time-Aware - first feasible slot/strategy
3. Best-Fit Time-Aware - tightest fit to avoid fragmentation
4. DP (no warm-start) - optimal but slow
5. DP + Warm-Start - optimal with greedy upper bound
6. DP + K-Best - optimal with aggressive pruning
7. DP + Beam - optimal with diversity-preserving pruning

Configuration demonstrates impact of temporal constraints.
"""

import sys
import os
import tempfile
import subprocess
import time
import random


# Test configuration
NUM_REQUESTS = 80
NUM_BLOCKS = 10
NUM_SLOTS = 10
ERROR_THRESHOLD = 4
SLOT_DURATION_MIN = 30   # Each slot is 30 minutes
PARALLELISM = 3          # 4 requests can execute in parallel
SEED = 42

# Capacity tiers (set to None or empty list to disable)
ENABLE_CAPACITY_TIERS = False  # Set to False to disable capacity tiers
CAPACITY_TIERS = [
    (5, 1.0),
    (10, 2.0),
    (15, 4.0),
    (25, 8.0),
] if ENABLE_CAPACITY_TIERS else []

# Pruning configurations (relaxed)
KBEST_K = 3000
BEAM_K = 150


def generate_inputs(temp_dir):
    """Generate test input files"""
    
    print(f"Generating test inputs...")
    print(f"  Requests: {NUM_REQUESTS}")
    print(f"  Blocks: {NUM_BLOCKS}")
    print(f"  Slots: {NUM_SLOTS}")
    print(f"  Error threshold: {ERROR_THRESHOLD}%")
    print(f"  Slot duration: {SLOT_DURATION_MIN} minutes")
    print(f"  Parallelism: {PARALLELISM} concurrent requests")
    print(f"  Time capacity per slot: {SLOT_DURATION_MIN * PARALLELISM} request-minutes")
    print()
    
    # Requests with generous deadlines
    requests_file = os.path.join(temp_dir, 'requests.csv')
    random.seed(SEED)
    min_deadline = int(NUM_SLOTS * 0.7)
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
        f.write('0,20,High\n')     # Perfect quality, 20 min execution
        f.write('3,10,Medium\n')   # Good quality, 10 min execution
        f.write('6,5,Low\n')       # Fast execution, 5 min, poor quality
    
    print("Strategies:")
    print("  High:   0% error, 20 min duration")
    print("  Medium: 3% error, 10 min duration")
    print("  Low:    6% error,  5 min duration")
    print()
    
    # Carbon intensity: decreasing (later = greener)
    carbon_file = os.path.join(temp_dir, 'carbon.txt')
    carbon_pattern = [
        200, 180, 160, 140,  # Early: high carbon
        120, 100, 80, 70,    # Middle: medium
        60, 50               # Late: green
    ]
    
    with open(carbon_file, 'w') as f:
        for c in carbon_pattern:
            f.write(f'{c}\n')
    
    print(f"Carbon: {min(carbon_pattern)} to {max(carbon_pattern)} gCO2/kWh")
    print(f"  → Later slots greener")
    print()
    
    # Capacity tiers
    tiers_file = os.path.join(temp_dir, 'tiers.csv')
    if CAPACITY_TIERS:
        with open(tiers_file, 'w') as f:
            for capacity, factor in CAPACITY_TIERS:
                f.write(f'{capacity},{factor}\n')
        
        print(f"Capacity tiers: {len(CAPACITY_TIERS)} levels")
        for cap, fac in CAPACITY_TIERS:
            print(f"  {cap:3d} requests → {fac:.1f}x emission")
    else:
        # Create empty file or single tier with factor 1.0
        with open(tiers_file, 'w') as f:
            f.write('999999,1.0\n')
        print("Capacity tiers: DISABLED (emission factor always 1.0)")
    print()
    
    return requests_file, strategies_file, carbon_file, tiers_file


def run_command(label, cmd, timeout=120):
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
                stderr_lines = result.stderr.split('\n')[:10]
                print(f"Error: {chr(10).join(stderr_lines)}")
            return None
        
        # Parse output
        cost = None
        error = None
        loads = None
        times = None
        states = None
        
        for line in result.stdout.split('\n'):
            if line.startswith('COST:'):
                cost = float(line.split(':')[1].strip())
            elif line.startswith('ERROR:'):
                error = float(line.split(':')[1].strip())
            elif line.startswith('LOADS:'):
                loads = list(map(int, line.split(':')[1].strip().split(',')))
            elif line.startswith('TIMES:'):
                times = list(map(float, line.split(':')[1].strip().split(',')))
            elif line.startswith('STATES:'):
                states = int(line.split(':')[1].strip())
        
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
            'times': times,
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
    
    costs = [r['cost'] for r in results.values() if r and r['cost']]
    if not costs:
        print("No valid results")
        return
    
    optimal_cost = min(costs)
    
    # Table
    print(f"{'Method':<30} {'Cost':>12} {'Gap':>8} {'Error':>8} {'Time':>8} {'States':>12}")
    print(f"{'-'*30} {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")
    
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
    
    print(f"\n🏆 = Optimal solution")
    
    # Analysis
    print(f"\n{'='*80}")
    print("ANALYSIS")
    print(f"{'='*80}\n")
    
    greedy_res = results.get('Greedy Time')
    dp_res = results.get('DP (no warm-start)')
    dp_warm_res = results.get('DP + Warm-Start')
    
    if greedy_res and dp_res:
        if dp_res['cost'] < greedy_res['cost']:
            improvement = ((greedy_res['cost'] - dp_res['cost']) / greedy_res['cost'] * 100)
            print(f"✅ DP found better solution than Greedy!")
            print(f"   Improvement: {improvement:.1f}%")
            print(f"   Greedy: {greedy_res['cost']:,.0f}")
            print(f"   DP:     {dp_res['cost']:,.0f}")
        else:
            print(f"✓ Greedy matched DP optimal")
            print(f"   Both: {greedy_res['cost']:,.0f}")
        print()
    
    if dp_res and dp_warm_res:
        states_before = dp_res.get('states', 0)
        states_after = dp_warm_res.get('states', 0)
        if states_before > 0:
            reduction = ((states_before - states_after) / states_before * 100)
            speedup = dp_res['time'] / dp_warm_res['time'] if dp_warm_res['time'] > 0 else 0
            
            print(f"⚡ Warm-start efficiency:")
            print(f"   States: {states_before:,} → {states_after:,} ({reduction:.1f}% reduction)")
            print(f"   Time: {dp_res['time']:.2f}s → {dp_warm_res['time']:.2f}s ({speedup:.1f}x speedup)")
            
            if dp_warm_res['cost'] == dp_res['cost']:
                print(f"   ✓ Maintained optimality")
        print()
    
    # Time capacity analysis
    print(f"{'='*80}")
    print("TIME CAPACITY UTILIZATION")
    print(f"{'='*80}\n")
    
    slot_capacity = SLOT_DURATION_MIN * PARALLELISM
    
    for label, result in results.items():
        if result and result.get('times'):
            times = result['times']
            max_time = max(times) if times else 0
            avg_time = sum(times) / len(times) if times else 0
            utilization = max_time / slot_capacity * 100 if slot_capacity > 0 else 0
            
            overflows = sum(1 for t in times if t > slot_capacity)
            
            print(f"{label}:")
            print(f"  Max time used: {max_time:.1f}/{slot_capacity} min ({utilization:.1f}% utilization)")
            print(f"  Avg time used: {avg_time:.1f} min")
            if overflows > 0:
                print(f"  ⚠️  Time overflows in {overflows} slots!")
            print()


def main():
    """Main test execution"""
    
    print(f"{'='*80}")
    print("TIME-CAPACITY TIERS: COMPREHENSIVE COMPARISON")
    print(f"{'='*80}\n")
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    
    greedy_script = os.path.join(parent_dir, 'greedy_time.py')
    first_fit_script = os.path.join(parent_dir, 'first_fit_time.py')
    best_fit_script = os.path.join(parent_dir, 'best_fit_time.py')
    dp_script = os.path.join(parent_dir, 'dp_time.py')
    
    with tempfile.TemporaryDirectory() as temp_dir:
        
        req_file, strat_file, carbon_file, tiers_file = generate_inputs(temp_dir)
        
        results = {}
        
        # Common args
        common_args = [
            req_file, strat_file, carbon_file,
            str(NUM_SLOTS), str(NUM_BLOCKS), str(ERROR_THRESHOLD),
            str(SLOT_DURATION_MIN), str(PARALLELISM)
        ]
        
        # 1. Greedy Time-Aware
        out1 = os.path.join(temp_dir, 'greedy.csv')
        cmd = [sys.executable, greedy_script] + common_args + [out1, '--capacity-file', tiers_file]
        results['Greedy Time'] = run_command("1. GREEDY TIME-AWARE", cmd)
        
        if not results['Greedy Time']:
            print("⚠️ Greedy failed - cannot continue")
            return
        
        greedy_cost = results['Greedy Time']['cost']
        
        # 2. First-Fit
        out2 = os.path.join(temp_dir, 'firstfit.csv')
        cmd = [sys.executable, first_fit_script] + common_args + [out2, '--capacity-file', tiers_file]
        results['First-Fit Time'] = run_command("2. FIRST-FIT TIME-AWARE", cmd)
        
        # 3. Best-Fit
        out3 = os.path.join(temp_dir, 'bestfit.csv')
        cmd = [sys.executable, best_fit_script] + common_args + [out3, '--capacity-file', tiers_file]
        results['Best-Fit Time'] = run_command("3. BEST-FIT TIME-AWARE", cmd)
        
        # 4. DP no warm-start
        out4 = os.path.join(temp_dir, 'dp_nowarm.csv')
        cmd = [sys.executable, dp_script] + common_args + [out4, '--capacity-file', tiers_file]
        results['DP (no warm-start)'] = run_command("4. DP (NO WARM-START)", cmd, timeout=180)
        
        # 5. DP with warm-start
        out5 = os.path.join(temp_dir, 'dp_warm.csv')
        cmd = [sys.executable, dp_script] + common_args + [out5, '--capacity-file', tiers_file,
               '--upper-bound', str(greedy_cost)]
        results['DP + Warm-Start'] = run_command("5. DP + WARM-START", cmd)
        
        # 6. DP with K-Best
        out6 = os.path.join(temp_dir, 'dp_kbest.csv')
        cmd = [sys.executable, dp_script] + common_args + [out6, '--capacity-file', tiers_file,
               '--upper-bound', str(greedy_cost), '--pruning', 'kbest', '--pruning-k', str(KBEST_K)]
        results['DP + K-Best'] = run_command("6. DP + K-BEST PRUNING", cmd)
        
        # 7. DP with Beam
        out7 = os.path.join(temp_dir, 'dp_beam.csv')
        cmd = [sys.executable, dp_script] + common_args + [out7, '--capacity-file', tiers_file,
               '--upper-bound', str(greedy_cost), '--pruning', 'beam', '--pruning-k', str(BEAM_K)]
        results['DP + Beam'] = run_command("7. DP + BEAM SEARCH", cmd)
        
        # Comparison
        print_comparison(results)
    
    print(f"\n✅ Test completed!\n")


if __name__ == '__main__':
    main()
