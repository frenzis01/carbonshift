# Online2 Changelog

## Version 0.1.0 - Initial Release (April 23, 2026)

### Added

#### Core Framework
- **config.py** - Centralized configuration system
  - Batch size, time slot parameters
  - Strategy definitions with error/duration tradeoffs
  - Error budget constraints (sliding 11-slot window)
  - Capacity tiers with rebound effect multipliers
  - DP solver parameters (pruning strategy, timeout, etc.)

- **shared_state.py** - Thread-safe shared state container
  - Request queue for pending requests
  - Assignment tracking (current and historical)
  - Error budget per-slot tracking
  - Sliding window error calculation
  - Thread-safe operations with RLock
  - CSV export functionality

- **request_generator.py** - Background request producer
  - Configurable request generation rate
  - Random deadline assignment (2-8 slots)
  - Daemon thread for background execution
  - Graceful shutdown support

- **scheduler.py** - Batch processing scheduler
  - Batch size awareness (waits for N requests)
  - Placeholder DP solver (current: naive greedy)
  - Capacity tier multiplier calculation
  - Error window validation framework
  - Thread-safe batch processing

- **main.py** - System orchestrator
  - Component initialization and startup
  - Background thread management
  - Signal handling (SIGINT, SIGTERM)
  - Statistics monitoring and display
  - CSV result export

#### Documentation
- **README.md** - Complete user guide
  - Architecture overview
  - Configuration quick reference
  - Running instructions
  - CSV output format
  - Future enhancement roadmap

- **ARCHITECTURE.md** - Technical deep-dive
  - System architecture diagrams
  - Data structure definitions
  - Batch scheduling algorithm
  - Error budget window explanation
  - Capacity tier rebound effect details
  - Thread safety guarantees
  - Performance characteristics
  - Extension points for customization

- **IMPLEMENTATION_PLAN.md** - Development roadmap
  - Completed phases (Phase 1)
  - In-progress tasks (Phase 2-4)
  - Future work (Phase 5-7)
  - Timeline estimates
  - Success criteria

### Project Structure
```
online2/
├── config.py                    # Configuration parameters
├── shared_state.py              # Thread-safe state container
├── request_generator.py         # Request producer
├── scheduler.py                 # Batch scheduler (DP placeholder)
├── main.py                      # System orchestrator
├── __init__.py                  # Module exports
├── README.md                    # User guide
├── ARCHITECTURE.md              # Technical documentation
├── IMPLEMENTATION_PLAN.md       # Development roadmap
├── CHANGELOG.md                 # This file
└── tests/
    └── README.md               # Test planning
```

### Configuration Features
- **Batch processing**: Configurable batch size (N >= 1)
- **Time slots**: 10-second slots, 24 total (configurable)
- **Strategies**: 3 strategies with different error/duration tradeoffs
  - Accurate: 1% error, 300s duration
  - Balanced: 2.5% error, 120s duration
  - Fast: 5% error, 30s duration
- **Error budget**: 3% max average in 11-slot window
- **Capacity tiers**: Rebound effect with 4 tiers (1000-3000+ reqs)
- **DP options**: Beam Search or K-Best pruning (placeholder)
- **Request generation**: 5 requests/slot average rate

### Known Limitations
- DP solver is placeholder (naive greedy), will be replaced with real DP
- Error window validation incomplete, awaits Phase 3
- Capacity tier multipliers not yet tested
- No request prioritization
- No backpressure handling
- Single machine only (no distribution)
- No persistent state (no crash recovery)

### Next Steps (Phase 2)
- [ ] Integrate carbonshift DP module
- [ ] Implement batch solving algorithm
- [ ] Test with small batches (N=3)
- [ ] Performance profiling
- [ ] Integration tests

### Testing Status
- ✅ Code structure complete and importable
- ✅ Thread safety design verified
- 🔄 Placeholder implementation runnable
- ⏸️ Full test suite pending Phase 2

---

**Created**: April 23, 2026  
**Status**: Framework Complete, DP Integration Pending  
**Maintainer**: Copilot
