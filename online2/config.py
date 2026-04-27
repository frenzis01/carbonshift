"""
Configuration for Online2 Batch Scheduler
"""

# ============================================================================
# BATCH PROCESSING PARAMETERS
# ============================================================================

# Number of requests to batch before scheduling
BATCH_SIZE = 3

# ============================================================================
# TIME SLOT PARAMETERS
# ============================================================================

# Duration of each time slot in seconds
# For testing: 10 seconds per slot
# In production: may be minutes or hours
SLOT_DURATION_SECONDS = 10

# Total number of time slots in the planning horizon
TOTAL_SLOTS = 24

# ============================================================================
# STRATEGY PARAMETERS
# ============================================================================

STRATEGIES = [
    {"name": "Accurate", "error": 1.0, "duration": 60},    # 1 min
    {"name": "Balanced", "error": 2.5, "duration": 30},    # 1/2 min
    {"name": "Fast", "error": 5.0, "duration": 10},         # 10 sec
]

# ============================================================================
# ERROR BUDGET PARAMETERS
# ============================================================================

# Maximum average error allowed in the sliding window.
MAX_ERROR_THRESHOLD = 4.0  # %

# Window size for error calculation (symmetric around current slot)
ERROR_WINDOW_PAST = 5
ERROR_WINDOW_FUTURE = 8
ERROR_WINDOW_SIZE = ERROR_WINDOW_PAST + 1 + ERROR_WINDOW_FUTURE

# Requests cannot be placed beyond current_slot + ASSIGNMENT_MAX_FUTURE_SLOTS.
# Keep this aligned with ERROR_WINDOW_FUTURE unless you explicitly want a smaller
# placement horizon.
ASSIGNMENT_MAX_FUTURE_SLOTS = 8

# Virtual pre-history for early iterations:
# for current_slot < ERROR_WINDOW_PAST, we assume virtual past slots (-W..-1)
# with request counts tied to the known arrival rate.
# This avoids an empty baseline at startup.
PREHISTORY_USE_VIRTUAL_PAST = True
PREHISTORY_ERROR_RATIO_OF_THRESHOLD = 0.75  # avg error = threshold * ratio
PREHISTORY_STOCHASTIC_COUNTS = True
PREHISTORY_RANDOM_SEED = 4242

# ============================================================================
# CAPACITY TIERS (REBOUND EFFECT)
# ============================================================================

# Capacity tiers: (max_requests, carbon_multiplier)
# If slot receives more than this many requests, carbon emissions multiply
CAPACITY_TIERS = [
    {"max_requests": 10, "multiplier": 1.5},
    {"max_requests": 20, "multiplier": 2.0},
    {"max_requests": 30, "multiplier": 5.0},
    {"max_requests": float('inf'), "multiplier": 3.0},
]

# ============================================================================
# DP SOLVER PARAMETERS
# ============================================================================

# Pruning strategy: 'kbest' or 'beam' or 'None' (no pruning)
# DP_PRUNING_STRATEGY = 'beam'
DP_PRUNING_STRATEGY = 'None'

# Number of states to keep during pruning
DP_PRUNING_K = 150

# Maximum seconds for DP solver per batch
DP_TIMEOUT = 7.0

# If True, assignments already made on future slots are fixed and considered as
# baseline load/error by the DP.
# If False, those future assignments are included in the optimization and can
# be moved.
DP_LOCK_FUTURE_ASSIGNMENTS = True

# If strict error-window DP is infeasible, allow one relaxed retry.
# Disable to enforce hard-threshold behavior only.
DP_ALLOW_RELAXED_ERROR_RETRY = True

# When relaxed retry is enabled, prefer the minimum-error strategy(ies) so the
# system can recover from a violated baseline instead of drifting to high error.
DP_RELAXED_RETRY_PREFER_MIN_ERROR = True

# Behavior when strict infeasibility is caused by an error baseline that is
# difficult to recover right after the window slides:
# - "min_error_recovery": assign with minimum-error strategy on recovery steps
# - "carryover_last_slot": use mock carryover from the slot that just left window
# - "forecast_mock_current_slot": use mock expected arrivals for current slot
INFEASIBILITY_RECOVERY_MODE = "forecast_mock_current_slot"

# ============================================================================
# REQUEST GENERATION PARAMETERS
# ============================================================================

# Predicted/known arrival rate (requests per slot), used by:
# - request generator
# - virtual pre-history baseline
PREDICTED_REQUESTS_PER_SLOT = 20.0

# Backward-compatible alias used across the codebase.
REQUESTS_PER_SLOT = PREDICTED_REQUESTS_PER_SLOT

# Gaussian variability factor used both in generation and pre-history sampling:
# sigma = max(1, rate * REQUEST_RATE_STD_FACTOR)
REQUEST_RATE_STD_FACTOR = 0.5

# Deadline range for generated requests (in slots from arrival)
DEADLINE_MIN_SLACK = 0
DEADLINE_MAX_SLACK = 8

# ============================================================================
# THREADING & CONCURRENCY
# ============================================================================

# Number of worker threads for scheduling
NUM_SCHEDULER_THREADS = 1

# Timeout for queue operations (seconds)
QUEUE_TIMEOUT = 1.0

# ============================================================================
# LOGGING & OUTPUT
# ============================================================================

# Enable detailed logging
VERBOSE = True

# Output file for scheduling decisions
OUTPUT_FILE = "/tmp/online2_assignments.csv"

# Enable per-solver execution logging (CSV)
ENABLE_SOLVER_LOGGING = True

# Solver log files
SOLVER_RUNS_FILE = "/tmp/online2_solver_runs.csv"
SOLVER_ASSIGNMENTS_FILE = "/tmp/online2_solver_assignments.csv"
SOLVER_SLOT_METRICS_FILE = "/tmp/online2_solver_slot_metrics.csv"

# Strict-infeasibility debug log:
# captures the state when strict error-window constraints reject a batch
# before relaxed retry/fallback.
ENABLE_INFEASIBILITY_DEBUG_LOGGING = True
SOLVER_INFEASIBLE_DEBUG_FILE = "/tmp/online2_solver_infeasible_debug.csv"
