#!/usr/bin/env python3
"""
Optimal Comparison Test: Compare Online Schedulers vs Optimal ILP Solution

Compares online/heuristic schedulers against the optimal offline solution
from carbonshift.py (batch ILP solver).
"""

import sys
import os
import random
import subprocess
import tempfile
from typing import List, Dict, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from online.request_predictor import MockRequestPredictor
from online.heuristics import (
    GreedyCarbonLookahead,
    ProbabilisticSlackScheduler,
    Request,
    Strategy
)
from online.rolling_window_ilp import RollingWindowILPScheduler


def generate_batch_requests(n: int, time_slots: int = 24, seed: int = 42) -> List[Request]:
    """Generate a batch of requests for offline optimization"""
    random.seed(seed)
    requests = []
    
    for i in range(n):
        arrival_time = random.randint(0, time_slots - 1)
        slack = random.randint(2, 6)
        deadline = min(arrival_time + slack, time_slots - 1)
        
        requests.append(Request(
            id=i,
            arrival_time=arrival_time,
            deadline=deadline
        ))
    
    return requests


def solve_optimal_carbonshift(requests: List[Request], 
                              strategies: List[Strategy],
                              carbon: List[float],
                              error_threshold: float = 5.0,
                              timeout: int = 60) -> Tuple[Dict[int, Tuple[int, str]], float]:
    """
    Solve using carbonshift.py (optimal batch ILP).
    
    carbonshift.py expects:
    - input_requests: CSV with deadlines (comma-separated)
    - input_strategies: CSV with error,duration,name (with header)
    - input_co2: text file with one carbon value per line
    - delta: number of time slots
    - beta: number of blocks (1 per request in our case)
    - error: error threshold
    - output_assignment: output file
    """
    print("  Solving optimal solution with carbonshift.py...")
    
    # Create temporary directory for files
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write input_requests.csv: comma-separated deadlines
        requests_file = os.path.join(tmpdir, 'input_requests.csv')
        with open(requests_file, 'w') as f:
            deadlines = [str(req.deadline) for req in requests]
            f.write(','.join(deadlines) + '\n')
        
        # Write input_strategies.csv: error,duration,name with header
        strategies_file = os.path.join(tmpdir, 'input_strategies.csv')
        with open(strategies_file, 'w') as f:
            f.write('error,duration,name\n')
            for s in strategies:
                f.write(f'{s.error},{s.duration},{s.name}\n')
        
        # Write input_co2.txt: one carbon value per line
        carbon_file = os.path.join(tmpdir, 'input_co2.txt')
        with open(carbon_file, 'w') as f:
            for c in carbon:
                f.write(f'{int(c)}\n')
        
        # Output file
        output_file = os.path.join(tmpdir, 'output_assignment.csv')
        
        # Run carbonshift.py
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        carbonshift_path = os.path.join(project_root, 'carbonshift.py')
        
        if not os.path.exists(carbonshift_path):
            print(f"  ERROR: carbonshift.py not found at {carbonshift_path}")
            return {}, float('inf')
        
        try:
            # Arguments: input_requests input_strategies input_co2 delta beta error output_assignment
            result = subprocess.run(
                [
                    sys.executable, carbonshift_path,
                    requests_file,
                    strategies_file,
                    carbon_file,
                    str(len(carbon)),  # delta: number of time slots
                    str(len(requests)),  # beta: number of blocks (one per request)
                    str(int(error_threshold)),  # error threshold
                    output_file
                ],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            if result.returncode != 0:
                print(f"  ERROR: carbonshift.py failed with code {result.returncode}")
                print(f"  stderr: {result.stderr[:500]}")
                return {}, float('inf')
            
            # Parse output file
            # Expected format: CSV with header: request_id,strategy,time_slot,emission,error
            # Followed by metrics like: all_emissions:value
            assignments = {}
            total_emissions = 0
            
            if os.path.exists(output_file):
                with open(output_file, 'r') as f:
                    lines = f.readlines()
                    
                    # Parse CSV rows
                    in_data = True
                    for line in lines:
                        line = line.strip()
                        
                        # Skip header
                        if 'request_id' in line.lower():
                            continue
                        
                        # Check if we reached metrics section
                        if ':' in line and ',' not in line:
                            in_data = False
                            # Parse metrics
                            if 'all_emissions:' in line:
                                try:
                                    total_emissions = float(line.split(':')[1].strip())
                                except:
                                    pass
                            continue
                        
                        # Parse data rows: request_id,strategy,time_slot,emission,error
                        if in_data and ',' in line:
                            parts = line.split(',')
                            if len(parts) >= 3:
                                try:
                                    req_id = int(parts[0])
                                    strategy_name = parts[1].strip()
                                    time_slot = int(parts[2])
                                    
                                    assignments[req_id] = (time_slot, strategy_name)
                                except (ValueError, IndexError):
                                    continue
            
            if assignments:
                # If total_emissions wasn't in file, calculate it
                if total_emissions == 0:
                    for req_id, (slot, strategy_name) in assignments.items():
                        strategy = next(s for s in strategies if s.name == strategy_name)
                        # Match carbonshift.py emission calculation
                        total_emissions += carbon[slot] * strategy.duration / 3600 * 0.05
                
                print(f"  Optimal solution found: {total_emissions:,.0f} emissions ({len(assignments)} requests)")
                return assignments, total_emissions
            else:
                print("  WARNING: Could not parse optimization results")
                return {}, float('inf')
            
        except subprocess.TimeoutExpired:
            print(f"  ERROR: carbonshift.py timed out after {timeout}s")
            return {}, float('inf')
        except Exception as e:
            print(f"  ERROR: {e}")
            return {}, float('inf')


def calculate_emissions_from_assignments(assignments: Dict[int, Tuple[int, str]], 
                                         strategies: List[Strategy], 
                                         carbon: List[float]) -> Tuple[float, float]:
    """
    Calculate total emissions and average error
    
    Emission calculation matches carbonshift.py power model for fair comparison:
    emission = carbon[gCO2/kWh] * duration[s] / 3600 * server_kwh[0.05kW] = gCO2
    """
    total_emissions = 0.0
    total_error = 0.0
    
    for req_id, (slot, strategy_name) in assignments.items():
        strategy = next(s for s in strategies if s.name == strategy_name)
        # Match carbonshift.py emission calculation
        emission = carbon[slot] * strategy.duration / 3600 * 0.05
        total_emissions += emission
        total_error += strategy.error
    
    avg_error = total_error / len(assignments) if assignments else 0.0
    return total_emissions, avg_error


def test_online_scheduler(scheduler_name: str, 
                         scheduler,
                         requests: List[Request],
                         strategies: List[Strategy],
                         carbon: List[float]) -> Dict[int, Tuple[int, str]]:
    """Test an online scheduler"""
    print(f"  Testing {scheduler_name}...")
    
    assignments = {}
    for req in requests:
        slot, strategy = scheduler.schedule(req, current_time=req.arrival_time)
        assignments[req.id] = (slot, strategy)
    
    return assignments


def run_comparison(n_requests: int, seed: int = 42):
    """Run comparison between optimal and online schedulers"""
    print(f"\n{'='*80}")
    print(f"OPTIMAL COMPARISON: {n_requests} requests")
    print(f"{'='*80}")
    print("\nNote: All schedulers now use the same emission calculation as carbonshift.py:")
    print("      emission = carbon[gCO2/kWh] * duration[s] / 3600 * 0.05kW = gCO2")
    print("      This ensures fair and accurate comparison.")
    print()
    
    # Setup
    strategies = [
        Strategy(name="High", error=0, duration=120),
        Strategy(name="Medium", error=2, duration=60),
        Strategy(name="Low", error=5, duration=30),
    ]
    
    carbon = [
        120, 110, 100, 95, 90, 100,
        110, 130, 160, 180, 200, 210,
        200, 190, 180, 170, 180, 200,
        220, 210, 190, 170, 150, 130
    ]
    
    predictor = MockRequestPredictor(
        base_load=50.0,
        morning_peak_hour=9,
        evening_peak_hour=19,
        seed=seed
    )
    
    # Generate requests
    print(f"\nGenerating {n_requests} requests...")
    requests = generate_batch_requests(n_requests, time_slots=24, seed=seed)
    print(f"  Generated {len(requests)} requests")
    
    # Solve optimal
    print(f"\n{'─'*80}")
    print("OPTIMAL SOLUTION (carbonshift.py - batch ILP)")
    print(f"{'─'*80}")
    
    optimal_assignments, optimal_emissions = solve_optimal_carbonshift(
        requests, strategies, carbon, error_threshold=5.0, timeout=120
    )
    
    if not optimal_assignments:
        print("\n⚠ Could not compute optimal solution - skipping comparison")
        return
    
    optimal_emissions_calc, optimal_error = calculate_emissions_from_assignments(
        optimal_assignments, strategies, carbon
    )
    
    # Use calculated emissions if parsing failed
    if optimal_emissions == 0 or optimal_emissions == float('inf'):
        optimal_emissions = optimal_emissions_calc
    
    print(f"  Emissions: {optimal_emissions:,.0f}")
    print(f"  Average error: {optimal_error:.2f}%")
    
    # Test online schedulers
    results = []
    
    # 1. GreedyCarbonLookahead
    print(f"\n{'─'*80}")
    print("ONLINE SCHEDULER 1: GreedyCarbonLookahead")
    print(f"{'─'*80}")
    
    scheduler = GreedyCarbonLookahead(
        strategies=strategies,
        carbon=carbon,
        capacity=100,
        pressure_weight=0.5,
        error_threshold=5.0,
        predictor=predictor
    )
    
    assignments = test_online_scheduler("GreedyCarbonLookahead", scheduler, 
                                       requests, strategies, carbon)
    emissions, avg_error = calculate_emissions_from_assignments(
        assignments, strategies, carbon
    )
    gap = ((emissions / optimal_emissions) - 1) * 100
    
    results.append({
        'name': 'GreedyCarbonLookahead',
        'emissions': emissions,
        'error': avg_error,
        'gap': gap
    })
    
    print(f"  Emissions: {emissions:,.0f} ({gap:+.2f}% vs optimal)")
    print(f"  Average error: {avg_error:.2f}%")
    
    # 2. ProbabilisticSlack
    print(f"\n{'─'*80}")
    print("ONLINE SCHEDULER 2: ProbabilisticSlack")
    print(f"{'─'*80}")
    
    scheduler = ProbabilisticSlackScheduler(
        strategies=strategies,
        carbon=carbon,
        capacity=100,
        slack_threshold=3,
        error_threshold=5.0,
        predictor=predictor
    )
    
    assignments = test_online_scheduler("ProbabilisticSlack", scheduler, 
                                       requests, strategies, carbon)
    emissions, avg_error = calculate_emissions_from_assignments(
        assignments, strategies, carbon
    )
    gap = ((emissions / optimal_emissions) - 1) * 100
    
    results.append({
        'name': 'ProbabilisticSlack',
        'emissions': emissions,
        'error': avg_error,
        'gap': gap
    })
    
    print(f"  Emissions: {emissions:,.0f} ({gap:+.2f}% vs optimal)")
    print(f"  Average error: {avg_error:.2f}%")
    
    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    
    print(f"\n{'Approach':<30} {'Emissions':>15} {'Gap vs Optimal':>15} {'Avg Error':>12}")
    print("─" * 80)
    
    print(f"{'OPTIMAL (batch ILP)':<30} {optimal_emissions:>15,.0f} {'—':>15} {optimal_error:>12.2f}%")
    print("─" * 80)
    
    for result in results:
        print(f"{result['name']:<30} {result['emissions']:>15,.0f} "
              f"{result['gap']:>14.2f}% {result['error']:>12.2f}%")
    
    # Find best online scheduler
    if results:
        best = min(results, key=lambda r: r['emissions'])
        print(f"\n{'='*80}")
        print(f"✓ Best online scheduler: {best['name']}")
        print(f"  Optimality gap: {best['gap']:.2f}%")
        print(f"  Emissions: {best['emissions']:,.0f} vs {optimal_emissions:,.0f} (optimal)")
        print(f"{'='*80}")


def main():
    print("="*80)
    print("Carbonshift - Optimal vs Online Scheduler Comparison")
    print("="*80)
    
    # Check if carbonshift.py exists
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    carbonshift_path = os.path.join(project_root, 'carbonshift.py')
    if not os.path.exists(carbonshift_path):
        print(f"\n⚠ WARNING: carbonshift.py not found at {carbonshift_path}")
        print("This test requires carbonshift.py to compute the optimal solution.")
        print("Exiting.")
        return
    
    # Run comparisons with different scales
    test_configs = [
        20,   # Small
        50,   # Medium
        100,  # Large
    ]
    
    for n_requests in test_configs:
        run_comparison(n_requests, seed=42)
    
    print("\n" + "="*80)
    print("All comparisons completed!")
    print("="*80)


if __name__ == '__main__':
    main()
