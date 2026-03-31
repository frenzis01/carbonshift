#!/usr/bin/env python3
"""
Stress Test: High Load Testing for Carbonshift Online Schedulers

Tests system behavior under high load with:
- Large number of requests (1000+)
- Various arrival patterns (bursty, uniform, gradual)
- Different capacity constraints
- Performance profiling (time, memory)
"""

import sys
import os
import random
import time
import tracemalloc
import tempfile
import subprocess
from typing import List, Dict, Tuple, Optional

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


def generate_uniform_requests(n: int, seed: int = 42) -> List[Request]:
    """Generate uniformly distributed requests over 24 hours"""
    random.seed(seed)
    requests = []
    
    for i in range(n):
        arrival_time = random.randint(0, 23)
        slack = random.randint(2, 8)
        deadline = min(arrival_time + slack, 23)
        
        requests.append(Request(
            id=i,
            arrival_time=arrival_time,
            deadline=deadline
        ))
    
    requests.sort(key=lambda r: r.arrival_time)
    return requests


def generate_bursty_requests(n: int, n_bursts: int = 3, seed: int = 42) -> List[Request]:
    """Generate requests with bursts at peak hours"""
    random.seed(seed)
    requests = []
    
    burst_hours = [9, 13, 19][:n_bursts]
    requests_per_burst = n // n_bursts
    
    req_id = 0
    for burst_hour in burst_hours:
        for _ in range(requests_per_burst):
            arrival_time = max(0, min(23, burst_hour + random.randint(-1, 1)))
            slack = random.randint(2, 6)
            deadline = min(arrival_time + slack, 23)
            
            requests.append(Request(
                id=req_id,
                arrival_time=arrival_time,
                deadline=deadline
            ))
            req_id += 1
    
    for _ in range(n - req_id):
        arrival_time = random.randint(0, 23)
        slack = random.randint(2, 8)
        deadline = min(arrival_time + slack, 23)
        
        requests.append(Request(
            id=req_id,
            arrival_time=arrival_time,
            deadline=deadline
        ))
        req_id += 1
    
    requests.sort(key=lambda r: r.arrival_time)
    return requests


def generate_gradual_requests(n: int, seed: int = 42) -> List[Request]:
    """Generate gradually increasing load throughout the day"""
    random.seed(seed)
    requests = []
    
    for i in range(n):
        while True:
            arrival_time = random.randint(0, 23)
            acceptance_prob = (arrival_time + 1) / 24
            if random.random() < acceptance_prob:
                break
        
        slack = random.randint(2, 8)
        deadline = min(arrival_time + slack, 23)
        
        requests.append(Request(
            id=i,
            arrival_time=arrival_time,
            deadline=deadline
        ))
    
    requests.sort(key=lambda r: r.arrival_time)
    return requests


def calculate_emissions(assignments: Dict[int, Tuple[int, str]], 
                       strategies: List[Strategy], 
                       carbon: List[float]) -> Tuple[float, float]:
    """Calculate total emissions and average error"""
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


def solve_optimal_carbonshift(requests: List[Request],
                              strategies: List[Strategy],
                              carbon: List[float],
                              error_threshold: float = 5.0,
                              timeout: int = 120) -> Tuple[Optional[Dict[int, Tuple[int, str]]], Optional[float]]:
    """
    Solve using carbonshift.py (optimal batch ILP).
    
    Returns:
        (assignments, total_emissions) or (None, None) if failed
    """
    print(f"  Solving optimal with carbonshift.py (timeout={timeout}s)...")
    
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
            print(f"  ⚠ carbonshift.py not found at {carbonshift_path}")
            return None, None
        
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
                print(f"  ⚠ carbonshift.py failed with code {result.returncode}")
                return None, None
            
            # Parse output file
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
                
                print(f"  ✓ Optimal: {total_emissions:.2f} emissions ({len(assignments)} requests)")
                return assignments, total_emissions
            else:
                print("  ⚠ Could not parse optimization results")
                return None, None
            
        except subprocess.TimeoutExpired:
            print(f"  ⚠ carbonshift.py timed out after {timeout}s")
            return None, None
        except Exception as e:
            print(f"  ⚠ Error running carbonshift.py: {e}")
            return None, None


def run_scheduler_test(scheduler_name: str, scheduler, requests: List[Request], 
                      strategies: List[Strategy], carbon: List[float]) -> Dict:
    """Run a single scheduler and measure performance"""
    print(f"\n  Testing {scheduler_name}...")
    
    tracemalloc.start()
    start_time = time.time()
    
    assignments = {}
    for req in requests:
        slot, strategy = scheduler.schedule(req, current_time=req.arrival_time)
        assignments[req.id] = (slot, strategy)
    
    elapsed_time = time.time() - start_time
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    emissions, avg_error = calculate_emissions(assignments, strategies, carbon)
    
    violations = 0
    for req in requests:
        slot, _ = assignments[req.id]
        if slot > req.deadline:
            violations += 1
    
    return {
        'name': scheduler_name,
        'emissions': emissions,
        'avg_error': avg_error,
        'time_sec': elapsed_time,
        'peak_mem_mb': peak_mem / (1024 * 1024),
        'violations': violations,
        'requests_per_sec': len(requests) / elapsed_time if elapsed_time > 0 else 0
    }


def run_stress_test(n_requests: int, workload_type: str, seed: int = 42):
    """Run stress test with specified parameters"""
    print(f"\n{'='*80}")
    print(f"STRESS TEST: {n_requests} requests, {workload_type} workload")
    print(f"{'='*80}")
    
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
    
    print(f"\nGenerating {n_requests} requests ({workload_type} pattern)...")
    if workload_type == "uniform":
        requests = generate_uniform_requests(n_requests, seed)
    elif workload_type == "bursty":
        requests = generate_bursty_requests(n_requests, n_bursts=3, seed=seed)
    elif workload_type == "gradual":
        requests = generate_gradual_requests(n_requests, seed=seed)
    else:
        raise ValueError(f"Unknown workload type: {workload_type}")
    
    print(f"  Generated {len(requests)} requests")
    print(f"  Time range: {min(r.arrival_time for r in requests)} - {max(r.arrival_time for r in requests)}")
    print(f"  Deadline range: {min(r.deadline for r in requests)} - {max(r.deadline for r in requests)}")
    
    # First, try to get optimal solution (with timeout based on problem size)
    print(f"\n{'─'*80}")
    print("OPTIMAL SOLUTION (carbonshift.py)")
    print(f"{'─'*80}")
    
    # Adjust timeout based on problem size
    if n_requests <= 100:
        timeout = 60
    elif n_requests <= 500:
        timeout = 120
    elif n_requests <= 1000:
        timeout = 300
    else:
        timeout = 600
    
    optimal_assignments, optimal_emissions = solve_optimal_carbonshift(
        requests, strategies, carbon, error_threshold=5.0, timeout=timeout
    )
    
    # Now test online schedulers
    print(f"\n{'─'*80}")
    print("ONLINE SCHEDULERS")
    print(f"{'─'*80}")
    
    results = []
    
    # Test schedulers
    for cap in [100, 1000]:
        scheduler = GreedyCarbonLookahead(
            strategies=strategies,
            carbon=carbon,
            capacity=cap,
            pressure_weight=0.5,
            error_threshold=5.0,
            predictor=predictor
        )
        results.append(run_scheduler_test(f"GreedyCarbonLookahead (cap={cap})", scheduler, 
                                         requests, strategies, carbon))
    
    for cap in [100, 1000]:
        scheduler = ProbabilisticSlackScheduler(
            strategies=strategies,
            carbon=carbon,
            capacity=cap,
            slack_threshold=3,
            error_threshold=5.0,
            predictor=predictor
        )
        results.append(run_scheduler_test(f"ProbabilisticSlack (cap={cap})", scheduler, 
                                         requests, strategies, carbon))
    
    # Print results
    print(f"\n{'='*80}")
    print("RESULTS")
    print(f"{'='*80}")
    
    # Show optimal if available
    if optimal_emissions is not None:
        print(f"\n📊 OPTIMAL (batch ILP): {optimal_emissions:.2f} emissions")
        print(f"{'─'*80}")
    
    print(f"\n{'Scheduler':<40} {'Emissions':>12} {'Gap':>8} {'Violations':>10} {'Time(s)':>10} {'Req/s':>10}")
    print("─" * 95)
    
    for result in results:
        gap_str = "—"
        if optimal_emissions is not None and optimal_emissions > 0:
            gap_pct = ((result['emissions'] - optimal_emissions) / optimal_emissions) * 100
            gap_str = f"+{gap_pct:.1f}%"
        
        print(f"{result['name']:<40} {result['emissions']:>12.2f} {gap_str:>8} {result['violations']:>10} "
              f"{result['time_sec']:>10.2f} {result['requests_per_sec']:>10.1f}")
    
    best_result = min(results, key=lambda r: r['emissions'])
    print(f"\n{'='*80}")
    print(f"✓ Best online scheduler: {best_result['name']}")
    print(f"  Emissions: {best_result['emissions']:.2f}")
    if optimal_emissions is not None:
        gap = ((best_result['emissions'] - optimal_emissions) / optimal_emissions) * 100
        print(f"  Gap vs optimal: +{gap:.1f}%")
    print(f"  Time: {best_result['time_sec']:.2f}s")
    print(f"  Throughput: {best_result['requests_per_sec']:.1f} requests/sec")
    print(f"  Deadline violations: {best_result['violations']}")
    print(f"{'='*80}")
    
    return results, optimal_emissions


def main():
    print("="*80)
    print("Carbonshift Online Scheduling - Stress Testing with Optimal Comparison")
    print("="*80)
    
    test_configs = [
        (100, "uniform"),
        (500, "uniform"),
        (1000, "uniform"),
        (1000, "bursty"),
        (1000, "gradual"),
        (2000, "uniform"),
        (5000, "uniform"),
    ]
    
    all_results = []
    optimal_results = []
    
    for n_requests, workload_type in test_configs:
        results, optimal_emissions = run_stress_test(n_requests, workload_type, seed=42)
        all_results.extend(results)
        optimal_results.append({
            'n_requests': n_requests,
            'workload': workload_type,
            'optimal_emissions': optimal_emissions
        })
    
    print("\n" + "="*80)
    print("OVERALL SUMMARY")
    print("="*80)
    
    print("\n📊 Optimal vs Best Online Scheduler:")
    print(f"{'Test':<30} {'Optimal':>12} {'Best Online':>15} {'Gap':>10}")
    print("─" * 70)
    
    # Group results by test configuration (4 schedulers per config)
    for i, (n_requests, workload_type) in enumerate(test_configs):
        opt_data = optimal_results[i]
        
        # Get the 4 results for this configuration
        start_idx = i * 4
        end_idx = start_idx + 4
        config_results = all_results[start_idx:end_idx]
        
        if config_results:
            best_online = min(config_results, key=lambda r: r['emissions'])
            test_name = f"{n_requests} {workload_type}"
            
            if opt_data['optimal_emissions'] is not None:
                gap = ((best_online['emissions'] - opt_data['optimal_emissions']) / 
                      opt_data['optimal_emissions']) * 100
                print(f"{test_name:<30} {opt_data['optimal_emissions']:>12.2f} "
                      f"{best_online['emissions']:>15.2f} {gap:>9.1f}%")
            else:
                print(f"{test_name:<30} {'—':>12} {best_online['emissions']:>15.2f} {'—':>10}")
    
    print("\n" + "="*80)
    print("✅ Stress testing with optimal comparison complete!")
    print("="*80)
    print(f"\nTotal tests run: {len(all_results)}")
    print(f"Average throughput: {sum(r['requests_per_sec'] for r in all_results) / len(all_results):.1f} requests/sec")
    print(f"Peak memory usage: {max(r['peak_mem_mb'] for r in all_results):.1f} MB")
    print("="*80)


if __name__ == '__main__':
    main()
