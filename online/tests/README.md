# Online Scheduler Comparison Tests

Comprehensive test suite for comparing different Carbonshift online scheduling approaches.

## Test Files

### Core Comparison Tests

#### `test_scheduler_comparison.py`
**Purpose**: Compare all available online scheduling approaches

Demonstrates and compares:
- **Baseline** - Immediate scheduling with highest quality
- **GreedyCarbonLookahead** - Fast heuristic with carbon awareness
- **ProbabilisticSlack** - Deadline-aware heuristic
- **Rolling Window ILP** - Near-optimal using ILP

**Usage**:
```bash
python test_scheduler_comparison.py
```

**Duration**: ~5 seconds  
**Requests**: 50

---

#### `test_stress.py`
**Purpose**: High-load stress testing with optimal comparison

Tests system behavior under various load conditions (100-5000 requests) with different arrival patterns:
- **Uniform**: Random distribution across 24 hours
- **Bursty**: Concentrated arrivals at peak hours (9 AM, 1 PM, 7 PM)
- **Gradual**: Increasing load throughout the day

For each test, computes the **optimal solution** using carbonshift.py and measures the **optimality gap** for each online scheduler.

**Performance Metrics**:
- Total emissions vs optimal baseline
- Optimality gap (%)
- Execution time & throughput
- Memory usage
- Deadline violations

**Requirements**:
- `carbonshift.py` in project root
- `ortools` package installed (for optimal solver)

**Usage**:
```bash
python test_stress.py  # Full suite (~5-10 minutes with optimal solving)
```

**Example Output**:
```
STRESS TEST: 500 requests, uniform workload
────────────────────────────────────────────
📊 OPTIMAL (batch ILP): 18.78 emissions

Scheduler                     Emissions      Gap  Violations  Time(s)  Req/s
─────────────────────────────────────────────────────────────────────────────
GreedyCarbonLookahead (cap=1000)  28.46   +51.6%          0     0.05  10771.6
ProbabilisticSlack (cap=1000)     76.80  +308.9%          0     0.00 216112.1
```

**Duration**: ~5-10 minutes (includes optimal solving)  
**Tests**: 7 configurations (100, 500, 1000×3, 2000, 5000 requests)

---

#### `test_optimal_comparison.py`
**Purpose**: Compare online schedulers vs optimal offline solution

Uses `carbonshift.py` (batch ILP) to compute theoretical optimum and measures optimality gap for online schedulers.

**Important Note**: All schedulers (both optimal and online) now use the **same emission calculation formula** for fair comparison:

```python
emission = carbon[gCO2/kWh] * duration[seconds] / 3600 * 0.05kW = gCO2
```

This matches carbonshift.py's power consumption model where:
- `carbon[t]` = carbon intensity in gCO2 per kWh
- `duration` = execution time in seconds
- `/ 3600` = converts seconds to hours  
- `0.05` = typical server power consumption in kW (50 Watts)

The result is total emissions in grams of CO2.

**Requirements**:
- `carbonshift.py` in project root
- `ortools` package installed

**Usage**:
```bash
python test_optimal_comparison.py
```

**Duration**: ~120 seconds  
**Requests**: 20, 50, 100

**What to focus on**: The relative performance between online schedulers, not the absolute gap vs optimal.

---

### Test Runner

#### `run_all_tests.py`
Run all comparison tests sequentially with summary report.

**Usage**:
```bash
python run_all_tests.py
```

---

## Quick Start

```bash
# Run all tests
cd online/tests
python run_all_tests.py

# Or run individually
python test_scheduler_comparison.py  # Fast
python test_stress.py               # Load testing
python test_optimal_comparison.py   # Optimality gap
```

## Key Metrics

- **Emissions**: Total carbon emissions (gCO2) - lower is better
- **Average Error**: Quality trade-off (%) - bounded by threshold
- **Optimality Gap**: % above optimal (online schedulers only)
- **Throughput**: Requests processed per second
- **Memory**: Peak memory usage (MB)
- **Violations**: Deadline violations (should be 0)

## Expected Results

Good performance indicators:
- ✅ 30-80% emissions reduction vs baseline
- ✅ No deadline violations
- ✅ >100 req/sec throughput for heuristics
- ✅ <20% optimality gap

## Installation

```bash
# Install dependencies
pip install -r ../../requirements.txt

# Verify
python -c "import ortools; print('✓ ortools installed')"
```

## Troubleshooting

### "ModuleNotFoundError: No module named 'ortools'"
```bash
pip install ortools
```

### "carbonshift.py not found"
Ensure `carbonshift.py` is in the project root (two levels up from this directory).

### Test output too verbose
Redirect to file:
```bash
python test_scheduler_comparison.py > results.txt 2>&1
```

---

## Unit Tests

This directory also contains unit tests for individual modules:
- `test_heuristics.py` - Unit tests for heuristic schedulers
- `test_request_predictor.py` - Unit tests for request predictor
- `test_rolling_window_ilp.py` - Unit tests for ILP scheduler

Run with:
```bash
python -m pytest test_heuristics.py
```
