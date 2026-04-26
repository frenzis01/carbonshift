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
    {"name": "Accurate", "error": 1.0, "duration": 300},    # 5 min
    {"name": "Balanced", "error": 2.5, "duration": 120},    # 2 min
    {"name": "Fast", "error": 5.0, "duration": 30},         # 30 sec
]

# ============================================================================
# ERROR BUDGET PARAMETERS
# ============================================================================

# Maximum average error allowed in any sliding window of 10 slots
# (5 past + current + 5 future)
MAX_ERROR_THRESHOLD = 3.0  # %

# Window size for error calculation (symmetric around current slot)
ERROR_WINDOW_PAST = 5
ERROR_WINDOW_FUTURE = 5
ERROR_WINDOW_SIZE = ERROR_WINDOW_PAST + 1 + ERROR_WINDOW_FUTURE  # 11 total

# ============================================================================
# CAPACITY TIERS (REBOUND EFFECT)
# ============================================================================

# Capacity tiers: (max_requests, carbon_multiplier)
# If slot receives more than this many requests, carbon emissions multiply
CAPACITY_TIERS = [
    {"max_requests": 10, "multiplier": 1.0},
    {"max_requests": 20, "multiplier": 1.5},
    {"max_requests": 30, "multiplier": 4.0},
    {"max_requests": float('inf'), "multiplier": 3.0},
]

# ============================================================================
# DP SOLVER PARAMETERS
# ============================================================================

# Pruning strategy: 'kbest' or 'beam' or 'None' (no pruning)
DP_PRUNING_STRATEGY = 'beam'
# DP_PRUNING_STRATEGY = 'None'

# Number of states to keep during pruning
DP_PRUNING_K = 150

# Maximum seconds for DP solver per batch
DP_TIMEOUT = 7.0

# If True, assignments already made on future slots are fixed and considered as
# baseline load/error by the DP.
# If False, those future assignments are included in the optimization and can
# be moved.
DP_LOCK_FUTURE_ASSIGNMENTS = False

# ============================================================================
# REQUEST GENERATION PARAMETERS
# ============================================================================

# Rate of request arrivals per slot (average)
REQUESTS_PER_SLOT = 10

# Deadline range for generated requests (in slots from arrival)
DEADLINE_MIN_SLACK = 2
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
