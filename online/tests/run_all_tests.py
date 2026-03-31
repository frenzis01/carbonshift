#!/usr/bin/env python3
"""
Test Runner: Execute all comprehensive comparison tests

Runs all test suites in sequence:
1. Scheduler comparison
2. Stress testing
3. Optimal comparison (if dependencies available)
"""

import sys
import os
import subprocess
from pathlib import Path

# Color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def run_test(test_name: str, test_script: str) -> bool:
    """Run a single test script and report results"""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}Running: {test_name}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'='*80}{Colors.ENDC}\n")
    
    try:
        result = subprocess.run(
            [sys.executable, test_script],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=False,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            print(f"\n{Colors.OKGREEN}✓ {test_name} completed successfully{Colors.ENDC}")
            return True
        else:
            print(f"\n{Colors.FAIL}✗ {test_name} failed with exit code {result.returncode}{Colors.ENDC}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"\n{Colors.FAIL}✗ {test_name} timed out{Colors.ENDC}")
        return False
    except Exception as e:
        print(f"\n{Colors.FAIL}✗ {test_name} failed with error: {e}{Colors.ENDC}")
        return False


def main():
    print(f"{Colors.BOLD}{Colors.OKCYAN}")
    print("="*80)
    print("Carbonshift Comprehensive Test Suite Runner")
    print("="*80)
    print(Colors.ENDC)
    
    # Get test directory
    test_dir = Path(__file__).parent
    
    # Define tests to run
    tests = [
        ("Scheduler Comparison", test_dir / "test_scheduler_comparison.py"),
        ("Stress Testing", test_dir / "test_stress.py"),
        ("Optimal Comparison", test_dir / "test_optimal_comparison.py"),
    ]
    
    # Run all tests
    results = {}
    for test_name, test_script in tests:
        if not test_script.exists():
            print(f"{Colors.WARNING}⚠ Skipping {test_name} - file not found: {test_script}{Colors.ENDC}")
            results[test_name] = None
            continue
        
        results[test_name] = run_test(test_name, str(test_script))
    
    # Summary
    print(f"\n{Colors.BOLD}{Colors.HEADER}")
    print("="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(Colors.ENDC)
    
    total = len([r for r in results.values() if r is not None])
    passed = len([r for r in results.values() if r is True])
    failed = len([r for r in results.values() if r is False])
    skipped = len([r for r in results.values() if r is None])
    
    for test_name, result in results.items():
        if result is True:
            status = f"{Colors.OKGREEN}✓ PASSED{Colors.ENDC}"
        elif result is False:
            status = f"{Colors.FAIL}✗ FAILED{Colors.ENDC}"
        else:
            status = f"{Colors.WARNING}⊘ SKIPPED{Colors.ENDC}"
        
        print(f"  {test_name:<40} {status}")
    
    print(f"\n{Colors.BOLD}Total: {total + skipped} tests{Colors.ENDC}")
    print(f"  {Colors.OKGREEN}Passed: {passed}{Colors.ENDC}")
    print(f"  {Colors.FAIL}Failed: {failed}{Colors.ENDC}")
    if skipped > 0:
        print(f"  {Colors.WARNING}Skipped: {skipped}{Colors.ENDC}")
    
    print("\n" + "="*80)
    
    # Exit code
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
