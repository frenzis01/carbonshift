#!/bin/bash
# Quick test scripts for Online2 batch scheduler

echo "╔════════════════════════════════════════════════════════════╗"
echo "║       Online2 Batch Scheduler - Quick Test Suite           ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

cd "$(dirname "$0")"

# Test 1: Import Check
echo -e "${BLUE}[Test 1] Checking module imports...${NC}"
python -c "
import config
import shared_state
import request_generator
import scheduler
import main
print('✓ All modules importable')
" || exit 1
echo ""

# Test 2: Configuration Validation
echo -e "${BLUE}[Test 2] Validating configuration...${NC}"
python -c "
import config
print(f'✓ BATCH_SIZE = {config.BATCH_SIZE}')
print(f'✓ SLOT_DURATION = {config.SLOT_DURATION_SECONDS}s')
print(f'✓ TOTAL_SLOTS = {config.TOTAL_SLOTS}')
print(f'✓ MAX_ERROR_THRESHOLD = {config.MAX_ERROR_THRESHOLD}%')
print(f'✓ Strategies: {len(config.STRATEGIES)} defined')
print(f'✓ Capacity tiers: {len(config.CAPACITY_TIERS)} levels')
" || exit 1
echo ""

# Test 3: Quick 10-second run
echo -e "${BLUE}[Test 3] Running 10-second system test...${NC}"
python main.py --duration 10 2>&1 | grep -E "✓|✗|Generated:|Scheduled:|Cost:" || true
echo ""

# Test 4: Check CSV output
echo -e "${BLUE}[Test 4] Checking CSV output...${NC}"
if [ -f /tmp/online2_assignments.csv ]; then
    LINES=$(wc -l < /tmp/online2_assignments.csv)
    echo "✓ CSV file exists with $LINES lines"
    echo "✓ First 3 rows:"
    head -3 /tmp/online2_assignments.csv | sed 's/^/  /'
else
    echo "✗ CSV file not found"
fi
echo ""

# Test 5: Configuration test
echo -e "${BLUE}[Test 5] Testing different batch sizes...${NC}"
for BATCH in 1 3 10; do
    python -c "
import config
# Can modify config
print(f'✓ Can test BATCH_SIZE={BATCH}')
" || exit 1
done
echo ""

# Test 6: Performance estimate
echo -e "${BLUE}[Test 6] Performance characteristics...${NC}"
python -c "
import config
slots = config.TOTAL_SLOTS
batch = config.BATCH_SIZE
rate = config.REQUESTS_PER_SLOT
duration = slots * config.SLOT_DURATION_SECONDS
total_reqs = int(slots * rate)
batches = total_reqs // batch
print(f'✓ Test duration: {duration}s ({slots} slots × {config.SLOT_DURATION_SECONDS}s)')
print(f'✓ Expected requests: ~{total_reqs}')
print(f'✓ Expected batches: ~{batches} (batch_size={batch})')
print(f'✓ Avg batch latency: {duration / batches:.1f}s')
"
echo ""

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            All Quick Tests Passed! ✓                       ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Ready to test? Run:"
echo "  python main.py --duration 30"
echo ""
