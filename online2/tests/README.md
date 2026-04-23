# Online2 Tests

This directory will contain tests for the Online2 batch scheduler system.

## Test Plans

### Phase 1: Basic Functionality
- [ ] test_request_generator.py - Request generation at correct rate
- [ ] test_shared_state.py - Thread-safe state access
- [ ] test_scheduler_basic.py - Basic batch scheduling

### Phase 2: Integration
- [ ] test_end_to_end.py - Full system with N=1, 10s slots, 30s duration
- [ ] test_capacity_tiers.py - Verify rebound effect multipliers
- [ ] test_error_window.py - Validate error budget constraints

### Phase 3: Performance
- [ ] test_load_n1.py - Throughput with BATCH_SIZE=1
- [ ] test_load_n3.py - Throughput with BATCH_SIZE=3
- [ ] test_load_n10.py - Throughput with BATCH_SIZE=10

See parent README.md for configuration and running instructions.
