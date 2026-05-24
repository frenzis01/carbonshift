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
# FLAVOUR PARAMETERS
# ============================================================================

FLAVOURS = [
    {"name": "Accurate", "error": 0.0, "duration": 60},    # 1 min
    {"name": "Balanced", "error": 2.5, "duration": 30},    # 1/2 min
    {"name": "Fast", "error": 5.0, "duration": 10},         # 10 sec
]

# ============================================================================
# ERROR BUDGET PARAMETERS
# ============================================================================

# Maximum average error allowed in the sliding window.
MAX_ERROR_THRESHOLD = 4.0  # %

# Window size for error calculation (symmetric around current slot)
ERROR_WINDOW_PAST = 12
ERROR_WINDOW_FUTURE = 8
ERROR_WINDOW_SIZE = ERROR_WINDOW_PAST + 1 + ERROR_WINDOW_FUTURE

# Additional past slots with linearly decayed influence in error baseline.
# For K slots, weights are:
#   K/(K+1), (K-1)/(K+1), ..., 1/(K+1)
# Example K=6 => 6/7, 5/7, ..., 1/7
ERROR_WINDOW_PAST_DECAY_SLOTS = 0

# Requests cannot be placed beyond current_slot + ASSIGNMENT_MAX_FUTURE_SLOTS.
# Keep this aligned with ERROR_WINDOW_FUTURE unless you explicitly want a smaller
# placement horizon.
ASSIGNMENT_MAX_FUTURE_SLOTS = 8

# Global error constraint: in addition to the window constraint, enforce that
# the cumulative average error across ALL ever-assigned requests stays under
# MAX_ERROR_THRESHOLD.
# When HARD=True: filter flavours to those with error <= threshold when violated.
# When HARD=False: log a warning only.
GLOBAL_ERROR_CONSTRAINT_ENABLED = True
GLOBAL_ERROR_CONSTRAINT_HARD = True

# Virtual pre-history for early iterations:
# for current_slot < ERROR_WINDOW_PAST, we assume virtual past slots (-W..-1)
# with request counts tied to the known arrival rate.
# This avoids an empty baseline at startup.
PREHISTORY_USE_VIRTUAL_PAST = False
PREHISTORY_ERROR_RATIO_OF_THRESHOLD = 1.0  # avg error = threshold * ratio
# Forecast-policy synthetic error ratio for infeasibility recovery.
# Used only by INFEASIBILITY_RECOVERY_MODE="forecast_mock_current_slot":
# mock_error = MAX_ERROR_THRESHOLD * FORECAST_ERROR_RATIO_OF_THRESHOLD
FORECAST_ERROR_RATIO_OF_THRESHOLD = 1.0
PREHISTORY_STOCHASTIC_COUNTS = True
PREHISTORY_RANDOM_SEED = 4242
# Separate scaling factor for synthetic prehistory request counts used in
# benchmark scenario generation (independent from runtime infeasibility mocks).
PREHISTORY_MOCK_INFLUENCE = 0.4
CARBON_RANDOM_NOISE_AMPLITUDE = 40.0
# Period (in slots) of the sinusoidal carbon-intensity wave used in scenario
# generation.  A value of 24 models a 24-slot daily cycle.
CARBON_INTENSITY_CYCLE_SLOTS = 24


# ============================================================================
# CAPACITY TIERS (REBOUND EFFECT)
# ============================================================================

# Capacity tiers: (max_requests, carbon_multiplier)
# If slot receives more than this many requests, carbon emissions multiply
CAPACITY_TIERS = [
    {"max_requests": 30, "multiplier": 1.5},
    {"max_requests": 50, "multiplier": 2.0},
    {"max_requests": 80, "multiplier": 5.0},
    {"max_requests": float('inf'), "multiplier": 2.5},
]

# ============================================================================
# DP SOLVER PARAMETERS
# ============================================================================

# Pruning method: 'kbest' or 'beam' or 'None' (no pruning)
DP_PRUNING_METHOD = 'beam'

# Apply DP pruning only when pending batch size is >= this threshold.
# - 0: disable pruning entirely (even if DP_PRUNING_METHOD is set)
# - N>0: enable pruning only for batches with size >= N
DP_PRUNING_MIN_BATCH_SIZE = 5

# Number of states to keep during pruning
DP_PRUNING_K = 600

# Maximum seconds for DP solver per batch
DP_TIMEOUT = 7.0

# If True, assignments already made on future slots are fixed and considered as
# baseline load/error by the DP.
# If False, those future assignments are included in the optimization and can
# be moved.
DP_LOCK_FUTURE_ASSIGNMENTS = True

# If strict error-window DP is infeasible, allow one relaxed retry.
# Disable to enforce hard-threshold behavior only.
DP_ALLOW_RELAXED_ERROR_RETRY = False

# When relaxed retry is enabled, prefer the minimum-error flavour(s) so the
# system can recover from a violated baseline instead of drifting to high error.
DP_RELAXED_RETRY_PREFER_MIN_ERROR = True

# Behavior when strict infeasibility is caused by an error baseline that is
# difficult to recover right after the window slides:
# - "min_error_recovery": assign with minimum-error flavour on recovery steps
# - "carryover_last_slot": use mock carryover from the slot that just left window
# - "forecast_mock_current_slot": use mock expected arrivals for current slot
INFEASIBILITY_RECOVERY_MODE = "forecast_mock_current_slot"

# Scales the number of mock requests used in carryover/forecast recovery modes.
# Range [0, 1]: lower means less mock influence (more pessimistic).
# 1.0 = full mock influence, 0.0 = disable mock contribution.
INFEASIBILITY_MOCK_INFLUENCE = 0.4

# Optional fixed per-request error (%) for infeasibility synthetic mocks.
# - None: use policy-derived value (carryover slot average, or threshold*ratio).
# - >= 0: override policy-derived value with a fixed error.
INFEASIBILITY_MOCK_ERROR_PER_REQUEST = None

# Consecutive above-threshold window slots decay the effective mock influence:
# effective = max(0, base_influence - streak * decay_step)
# where streak counts consecutive slots whose baseline window avg error at slot
# start is above MAX_ERROR_THRESHOLD.
INFEASIBILITY_MOCK_INFLUENCE_DECAY_STEP = 0.2

# ============================================================================
# REQUEST GENERATION PARAMETERS
# ============================================================================

# Predicted/known arrival rate (requests per slot), used by:
# - request generator
# - virtual pre-history baseline
PREDICTED_REQUESTS_PER_SLOT = 60.0

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

# Maximum number of per-batch solver workers that can run simultaneously.
# Each worker thread is created for one batch and terminated when done.
MAX_BATCH_SOLVER_PARALLELISM = 5

# Legacy alias retained for compatibility in older scripts.
# New code should use MAX_BATCH_SOLVER_PARALLELISM.
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

# Visualization flags (used by visualize_solver_logs.py)
# If False, hide only the horizontal "Window avg (real)" line.
SHOW_WINDOW_AVG_REAL_LINE = True

# Strict-infeasibility debug log:
# captures the state when strict error-window constraints reject a batch
# before relaxed retry/fallback.
ENABLE_INFEASIBILITY_DEBUG_LOGGING = True
SOLVER_INFEASIBLE_DEBUG_FILE = "/tmp/online2_solver_infeasible_debug.csv"
