/// Batch scheduler — orchestrates the DP-based carbon-aware scheduling.
///
/// Mirrors `scheduler.py::BatchScheduler`.
///
/// # Concurrency model
/// A single background thread (the "main loop") polls the pending queue and
/// dispatches short-lived worker threads (one per batch).  All mutable
/// scheduler state is protected by `Arc<Mutex<SchedulerMutableState>>`.
/// The `DpSolver` is created fresh per worker so there is no shared mutable
/// solver state across concurrent batches.
use std::collections::{HashMap, HashSet};
use std::io::Write as IoWrite;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use std::sync::RwLock;

use crate::config::Config;
use crate::engine::qos::QosProfileId;
use crate::engine::solver::{
    CommitReport, SolverError, SolverRegistry, SolverRuntime, SolverScope,
};
use crate::engine::solver_algorithms::{DpAlgorithmConfig, register_all_solvers};
use crate::metrics_logger::MetricsLogger;
use crate::shared_state::{ClaimedRequestBatch, SharedState, SolverSnapshot};
use crate::types::{Assignment, Request};

mod forecast;
mod recovery;
mod solve;
#[cfg(test)]
mod tests;

pub use forecast::{advance_to_next_slot, generate_carbon_intensity_forecast};
#[cfg(test)]
use recovery::RecoveryState;
pub use solve::SolveContext;
use solve::build_solve_input_from_snapshot;
#[cfg(test)]
use solve::get_effective_pruning_mode;
#[cfg(test)]
use solve::{BatchSolveContext, select_batch_solver};

// ─── internal state types ────────────────────────────────────────────────────

#[derive(Debug, Default)]
struct SchedulerStats {
    batches_processed: u64,
    total_scheduled: u64,
    solver_runs: u64,
    solver_total_time_ms: f64,
    solver_total_requests: u64,
    last_solver_elapsed_ms: f64,
    peak_concurrent_workers: usize,
    /// Integral of active worker count over scheduler wall time, in worker-seconds.
    worker_time_area_secs: f64,
    /// Wall time during which at least one batch worker was active.
    worker_busy_time_secs: f64,
    started_at: Option<Instant>,
    last_worker_count_change: Option<Instant>,
    worker_busy_started_at: Option<Instant>,
    finished_at: Option<Instant>,
}

impl SchedulerStats {
    fn start(&mut self, now: Instant) {
        self.started_at = Some(now);
        self.last_worker_count_change = Some(now);
        self.worker_busy_started_at = None;
        self.finished_at = None;
        self.worker_time_area_secs = 0.0;
        self.worker_busy_time_secs = 0.0;
    }

    fn record_worker_count_change(
        &mut self,
        active_workers_before: usize,
        active_workers_after: usize,
        now: Instant,
    ) {
        let Some(last_change) = self.last_worker_count_change else {
            return;
        };
        self.worker_time_area_secs +=
            active_workers_before as f64 * now.duration_since(last_change).as_secs_f64();
        if active_workers_before == 0 && active_workers_after > 0 {
            self.worker_busy_started_at = Some(now);
        } else if active_workers_before > 0 && active_workers_after == 0 {
            if let Some(started_at) = self.worker_busy_started_at.take() {
                self.worker_busy_time_secs += now.duration_since(started_at).as_secs_f64();
            }
        }
        self.last_worker_count_change = Some(now);
    }

    fn finish(&mut self, active_workers: usize, now: Instant) {
        self.record_worker_count_change(active_workers, active_workers, now);
        if active_workers == 0 {
            self.finished_at = Some(now);
        }
    }

    fn average_concurrent_workers(&self, active_workers: usize, now: Instant) -> f64 {
        let Some(_) = self.started_at else {
            return 0.0;
        };
        let busy_time_secs = self.worker_busy_time_secs
            + if active_workers > 0 {
                self.worker_busy_started_at
                    .map(|started_at| now.duration_since(started_at).as_secs_f64())
                    .unwrap_or(0.0)
            } else {
                0.0
            };
        if busy_time_secs <= 0.0 {
            return 0.0;
        }
        let current_worker_time = if self.finished_at.is_none() {
            self.last_worker_count_change
                .map(|last_change| {
                    active_workers as f64 * now.duration_since(last_change).as_secs_f64()
                })
                .unwrap_or(0.0)
        } else {
            0.0
        };
        (self.worker_time_area_secs + current_worker_time) / busy_time_secs
    }
}

enum BatchWorkerOutcome {
    Committed,
    Infeasible,
}

/// All mutable scheduler state shared between the main loop and workers.
struct SchedulerMutableState {
    active_workers: usize,
    /// Used when a caller opts to serialize batches for each QoS profile.
    active_profiles: HashSet<QosProfileId>,
    /// Anti-storm guard scoped to a profile's pending queue.
    last_infeasible: HashMap<QosProfileId, (i32, usize)>,
    /// Round-robin cursor prevents a busy profile from monopolizing workers.
    last_dispatched_profile: Option<QosProfileId>,
    stats: SchedulerStats,
    solver_runtimes: HashMap<QosProfileId, Arc<dyn SolverRuntime>>,
}

// ─── public result type ──────────────────────────────────────────────────────

/// A read-only snapshot of scheduler throughput and worker concurrency.
#[derive(Debug, Clone)]
pub struct SchedulerStatistics {
    /// Number of worker batches that completed successfully.
    pub batches_processed: u64,
    /// Number of newly submitted requests committed by those batches.
    pub total_scheduled: u64,
    /// Number of solver runs recorded after a batch commits successfully.
    pub solver_runs: u64,
    /// Duration of the most recent solver invocation, in milliseconds.
    pub last_solver_elapsed_ms: f64,
    /// Total solver time divided by successful batches.
    pub avg_solver_ms_per_batch: f64,
    /// Total solver time divided by newly scheduled requests.
    pub avg_solver_ms_per_request: f64,
    /// Workers currently solving batches.
    pub active_batch_workers: usize,
    /// Configured upper bound on parallel batch workers.
    pub max_batch_parallelism: usize,
    /// Highest number of simultaneous workers observed since startup.
    pub peak_concurrent_workers: usize,
    /// Time-weighted average worker count while at least one batch worker is active.
    pub avg_concurrent_workers: f64,
}

// ─── BatchScheduler ──────────────────────────────────────────────────────────

pub struct BatchScheduler {
    shared_state: SharedState,
    cfg: Arc<Config>,
    /// Immutable factories shared with workers; profile runtimes are created later.
    solver_registry: Arc<SolverRegistry>,
    /// Pre-computed carbon-intensity forecast for the planning horizon.
    carbon_forecast: Arc<RwLock<Vec<f64>>>,
    running: Arc<AtomicBool>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    /// Whether multiple batches for one QoS profile may solve concurrently.
    allow_same_profile_parallelism: bool,
    pub metrics_logger: Arc<MetricsLogger>,
    main_thread: Option<JoinHandle<()>>,
}

/// Temporary deterministic seed until the online hazard seed is configurable.
const ONLINE_HAZARD_SEED: u64 = 345_542;

/// Build all engine factories once, using the same concrete algorithm
/// registrations for both scheduler constructors.
fn build_solver_registry(cfg: &Config) -> SolverRegistry {
    let dp_config = DpAlgorithmConfig {
        pruning_method: cfg.solver.dp_pruning_method.clone(),
        pruning_min_batch_size: cfg.solver.dp_pruning_min_batch_size,
        pruning_k: cfg.solver.dp_pruning_k,
        timeout_secs: cfg.solver.dp_timeout,
    };
    let mut registry = SolverRegistry::default();

    // Engine names are registered by this fixed composition function, so a
    // registration error here indicates a programming error (for example,
    // accidentally registering the same built-in name twice).
    register_all_solvers(&mut registry, dp_config, &cfg.swarm, ONLINE_HAZARD_SEED)
        .expect("built-in solver registry registration should succeed");

    registry
}

impl BatchScheduler {
    /// Create the scheduler and initialize global capacity policy before any
    /// worker can price assignments. The live service may already have done
    /// this during app setup, so initialization is intentionally idempotent.
    pub fn new(
        shared_state: SharedState,
        cfg: Arc<Config>,
        metrics_logger: Arc<MetricsLogger>,
        carbon_forecast: Arc<RwLock<Vec<f64>>>,
    ) -> Self {
        Self::new_inner(shared_state, cfg, metrics_logger, carbon_forecast, false)
    }

    /// Create a scheduler that may solve multiple batches from one QoS profile
    /// concurrently. Each worker uses the state snapshot captured atomically
    /// with its queue claim; proposals are committed as-is, with carbon costs
    /// recalculated against live slot occupancy.
    ///
    /// The `nshift` DP and online benchmark phases use this constructor to
    /// exercise the configured worker cap. The default constructor retains
    /// the live service's one-worker-per-profile behavior.
    pub fn new_with_parallel_profile_batches(
        shared_state: SharedState,
        cfg: Arc<Config>,
        metrics_logger: Arc<MetricsLogger>,
        carbon_forecast: Arc<RwLock<Vec<f64>>>,
    ) -> Self {
        Self::new_inner(shared_state, cfg, metrics_logger, carbon_forecast, true)
    }

    fn new_inner(
        shared_state: SharedState,
        cfg: Arc<Config>,
        metrics_logger: Arc<MetricsLogger>,
        carbon_forecast: Arc<RwLock<Vec<f64>>>,
        allow_same_profile_parallelism: bool,
    ) -> Self {
        // Offline schedulers have no REST AppState to initialize the runtime
        // policy. For the live service this is idempotent with AppState::new.
        shared_state.initialize_capacity_tiers(cfg.capacity_tiers.clone());
        let solver_registry = Arc::new(build_solver_registry(&cfg));
        let carbon_forecast = carbon_forecast;

        Self {
            shared_state,
            cfg: cfg.clone(),
            solver_registry,
            carbon_forecast,
            running: Arc::new(AtomicBool::new(false)),
            mutable: Arc::new(Mutex::new(SchedulerMutableState {
                active_workers: 0,
                active_profiles: HashSet::new(),
                last_infeasible: HashMap::new(),
                last_dispatched_profile: None,
                stats: SchedulerStats::default(),
                solver_runtimes: HashMap::new(),
            })),
            allow_same_profile_parallelism,
            metrics_logger,
            main_thread: None,
        }
    }

    /// Start the scheduler main-loop thread.
    pub fn start(&mut self) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }

        self.mutable.lock().unwrap().stats.start(Instant::now());

        let running = self.running.clone();
        let ss = self.shared_state.clone();
        let cfg = self.cfg.clone();
        let solver_registry = Arc::clone(&self.solver_registry);
        let forecast = self.carbon_forecast.clone();
        let mutable = self.mutable.clone();
        let ml = self.metrics_logger.clone();
        let allow_same_profile_parallelism = self.allow_same_profile_parallelism;

        if cfg.logging.verbose {
            println!(
                "[Scheduler] Started (batch_size={}, max_parallel={})",
                cfg.solver.batch_size, cfg.solver.max_batch_solver_parallelism
            );
        }

        self.main_thread = Some(std::thread::spawn(move || {
            main_loop(
                running,
                ss,
                cfg,
                solver_registry,
                forecast,
                mutable,
                ml,
                allow_same_profile_parallelism,
            );
        }));
    }

    /// Stop the scheduler and join all threads.
    pub fn stop(&mut self) {
        self.running.store(false, Ordering::SeqCst);
        if let Some(t) = self.main_thread.take() {
            let _ = t.join();
        }
        // Wait for active workers to finish (up to 5 s).
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if self.mutable.lock().unwrap().active_workers == 0 || Instant::now() > deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        {
            let mut mutable = self.mutable.lock().unwrap();
            let now = Instant::now();
            let active_workers = mutable.active_workers;
            mutable.stats.finish(active_workers, now);
        }
        if self.cfg.logging.verbose {
            let batches = self.mutable.lock().unwrap().stats.batches_processed;
            println!("[Scheduler] Stopped (processed {batches} batches)");
        }
    }

    /// Return a consistent snapshot of counters and worker concurrency.
    ///
    /// Solver averages use accumulated solver time and batch/request totals;
    /// worker concurrency is averaged over elapsed scheduler wall time.
    pub fn get_statistics(&self) -> SchedulerStatistics {
        let m = self.mutable.lock().unwrap();
        let runs = m.stats.solver_runs;
        let time_ms = m.stats.solver_total_time_ms;
        let reqs = m.stats.solver_total_requests;
        // Derive averages from accumulated totals rather than maintaining
        // counters on every worker completion; zero-run cases have no average.
        SchedulerStatistics {
            batches_processed: m.stats.batches_processed,
            total_scheduled: m.stats.total_scheduled,
            solver_runs: runs,
            last_solver_elapsed_ms: m.stats.last_solver_elapsed_ms,
            avg_solver_ms_per_batch: if runs > 0 { time_ms / runs as f64 } else { 0.0 },
            avg_solver_ms_per_request: if reqs > 0 { time_ms / reqs as f64 } else { 0.0 },
            active_batch_workers: m.active_workers,
            max_batch_parallelism: self.cfg.solver.max_batch_solver_parallelism,
            peak_concurrent_workers: m.stats.peak_concurrent_workers,
            avg_concurrent_workers: m
                .stats
                .average_concurrent_workers(m.active_workers, Instant::now()),
        }
    }

    /// Virtual elapsed time in seconds (shared with generator and monitor).
    pub fn shared_state_virtual_elapsed_secs(&self) -> f64 {
        self.shared_state.virtual_elapsed_secs()
    }
}

// ─── main loop ───────────────────────────────────────────────────────────────

/// Drive the scheduler until shutdown: synchronize virtual time, launch full
/// profile batches, flush smaller batches when a slot/timeout requires it,
/// and optionally skip idle slots in offline simulations.
///
/// This runs on the background thread created by [`BatchScheduler::start`].
/// Batch work itself runs on worker threads so the loop can continue updating
/// time, observing queue state, and admitting additional batches.
fn main_loop(
    running: Arc<AtomicBool>,
    shared_state: SharedState,
    cfg: Arc<Config>,
    solver_registry: Arc<SolverRegistry>,
    carbon_forecast: Arc<RwLock<Vec<f64>>>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    ml: Arc<MetricsLogger>,
    allow_same_profile_parallelism: bool,
) {
    let wall_start = Instant::now();
    let scheduler_thread = std::thread::current();
    let slot_ms = (cfg.effective_slot_duration_secs() * 1000.0) as u64;
    let eff_slot_dur = cfg.effective_slot_duration_secs();
    let mut last_flush_slot: i32 = -1;
    let mut flush_backlog = false;
    let mut last_skip_slot: i32 = -1;
    let mut last_progress_wall_ms: u64 = 0;
    let mut printed_progress = false;
    // Delta-based wall-clock tracking so we can *freeze* virtual time (instead
    // of losing it, only to burst-catch-up later) while the scheduler is busy.
    let mut last_wall_ms: u64 = wall_start.elapsed().as_millis() as u64;

    while running.load(Ordering::Relaxed) {
        let pending_count = shared_state.get_pending_count();
        let active_workers = mutable.lock().unwrap().active_workers;
        // Freeze the virtual clock whenever there is *any* outstanding work —
        // pending requests waiting to be dispatched, or workers still solving.
        // Freezing only at full saturation has a gap:
        // every time a worker finishes and is about to be immediately
        // replaced, `active_workers` momentarily dips below the parallelism
        // cap for the one loop iteration that reads it — and at batch_size=1
        // this refill happens once per *request*, so thousands of brief,
        // repeated "not quite saturated" windows per slot each leak a little
        // real time into the virtual clock. That leak accumulates well past
        // a request's deadline slack long before its own backlog is cleared,
        // which is exactly the "huge late-request pileup" failure mode.
        // Freezing on any backlog at all (matching the drain condition the
        // skip-forward branch below already requires) closes that gap: the
        // clock only ever advances once the system is genuinely caught up.
        // This alone doesn't starve the generator (a concern with an even
        // more aggressive freeze tried previously): the generator itself now
        // waits for the scheduler to drain each slot before emitting the
        // next one (see `generator.rs::wait_for_drain`), so both sides are
        // paced by the same "fully drained" signal.
        let system_busy = pending_count > 0 || active_workers > 0;

        // Keep virtual clock in sync with wall clock (skip mode may advance it
        // further). When `skip_empty_slots` is enabled (offline / non-realtime
        // simulation), freeze the virtual clock while there is outstanding
        // work: otherwise a slow/backlogged batch silently burns through
        // virtual slots and deadlines while it's still being computed, which
        // is exactly the "generator races ahead of the scheduler" failure
        // mode. In true realtime mode (`!skip_empty_slots`) wall time always
        // ticks 1:1, since falling behind there is meant to model genuine
        // real-world lateness.
        let wall_ms = wall_start.elapsed().as_millis() as u64;
        let delta_ms = wall_ms.saturating_sub(last_wall_ms);
        last_wall_ms = wall_ms;
        if !cfg.simulation.manual_clock && (!cfg.simulation.skip_empty_slots || !system_busy) {
            let current_vms = shared_state.virtual_elapsed_ms.load(Ordering::Relaxed);
            shared_state.set_virtual_elapsed_ms(current_vms + delta_ms);
        }

        let elapsed = shared_state.virtual_elapsed_secs();
        // Clamp to the last valid slot: once the generator has finished
        // emitting (end of scenario), wall-clock time keeps advancing while
        // this loop drains any remaining pending requests (Phase 2 in
        // `run_single_n`). Without this clamp, `current_slot` would keep
        // growing past `total_slots`, eventually exceeding the deadline of
        // any request still pending — making both the DP solver (which
        // rejects `current_slot >= window_size` outright) and
        // `greedy_fallback` (whose `current_slot..=deadline` range becomes
        // empty) permanently unable to place it, no matter how long Phase 2
        // waits.
        let current_slot = ((elapsed / eff_slot_dur) as i32).min(cfg.total_slots - 1);
        shared_state.set_current_slot(current_slot);

        let mut did_something = false;
        let close_slot_flush = shared_state.pending_flush_requested(current_slot);

        let has_full_profile_batch = shared_state
            .pending_profile_counts()
            .iter()
            .any(|(_, count)| *count >= cfg.solver.batch_size);
        if has_full_profile_batch && active_workers < cfg.solver.max_batch_solver_parallelism {
            if cfg.logging.verbose {
                println!(
                    "\n[Scheduler] Slot {current_slot}: {pending_count} pending, \
                     active_workers={active_workers}/{}",
                    cfg.solver.max_batch_solver_parallelism
                );
            }
            did_something = dispatch_batch_workers(
                current_slot,
                &shared_state,
                &cfg,
                &solver_registry,
                &carbon_forecast,
                &mutable,
                &ml,
                &running,
                false,
                allow_same_profile_parallelism,
                &scheduler_thread,
            );
        } else if pending_count > 0
            && active_workers < cfg.solver.max_batch_solver_parallelism
            && (close_slot_flush || current_slot > last_flush_slot || flush_backlog)
        {
            // Flush stranded partial batches either at slot end or when the
            // service closes a manual-clock slot before advancing its clock.
            if cfg.logging.verbose {
                println!("[Scheduler] Flush {pending_count} stale pending (slot={current_slot})");
            }
            did_something = dispatch_batch_workers(
                current_slot,
                &shared_state,
                &cfg,
                &solver_registry,
                &carbon_forecast,
                &mutable,
                &ml,
                &running,
                true,
                allow_same_profile_parallelism,
                &scheduler_thread,
            );
            last_flush_slot = current_slot;
            flush_backlog = has_retryable_profile_pending(&shared_state, &mutable, current_slot);
        } else if cfg.solver.batch_timeout_secs > 0.0
            && pending_count > 0
            && active_workers < cfg.solver.max_batch_solver_parallelism
        {
            // Batch timeout: flush if the oldest pending request has been
            // waiting longer than `batch_timeout_secs` virtual seconds.
            let virtual_ms = shared_state.virtual_elapsed_ms.load(Ordering::Relaxed);
            if let Some(age_ms) = shared_state.get_oldest_pending_age_ms(virtual_ms) {
                if age_ms as f64 >= cfg.solver.batch_timeout_secs * 1000.0 {
                    if cfg.logging.verbose {
                        println!(
                            "[Scheduler] Timeout flush {pending_count} pending \
                             (age={age_ms}ms, slot={current_slot})"
                        );
                    }
                    did_something = dispatch_batch_workers(
                        current_slot,
                        &shared_state,
                        &cfg,
                        &solver_registry,
                        &carbon_forecast,
                        &mutable,
                        &ml,
                        &running,
                        true,
                        allow_same_profile_parallelism,
                        &scheduler_thread,
                    );
                    flush_backlog =
                        has_retryable_profile_pending(&shared_state, &mutable, current_slot);
                }
            }
        } else if cfg.simulation.skip_empty_slots
            && pending_count == 0
            && active_workers == 0
            && current_slot < cfg.total_slots
            && current_slot > last_skip_slot
            && shared_state.generator_processed_slot() >= current_slot
        {
            // Fast-forward: jump the virtual clock to the next slot boundary
            // so the generator immediately feeds the next slot's requests.
            // We wait for `generator_processed_slot >= current_slot` to ensure
            // the generator has had a chance to add this slot's requests (even
            // if it has zero) before we skip, preventing the double-skip race.
            let next_ms = (current_slot as u64 + 1) * slot_ms;
            shared_state.set_virtual_elapsed_ms(next_ms);
            last_skip_slot = current_slot;
            if cfg.logging.verbose {
                println!(
                    "[Scheduler] ⏩ Skip slot {current_slot} → {}",
                    current_slot + 1
                );
            }
            did_something = true;
        }

        if pending_count == 0 && active_workers == 0 && close_slot_flush {
            shared_state.clear_pending_flush(current_slot);
        }

        // Sleep longer when truly idle; poll at 1ms when workers are running or
        // requests are pending so we dispatch as fast as the solver allows.
        let sleep_ms = if did_something || active_workers > 0 {
            1
        } else {
            10
        };
        std::thread::park_timeout(Duration::from_millis(sleep_ms));

        // Progress display (skipped in verbose mode to avoid mixing with debug lines).
        if !cfg.logging.verbose && cfg.logging.enable_progress_display {
            let wall_ms = wall_start.elapsed().as_millis() as u64;
            if wall_ms.saturating_sub(last_progress_wall_ms) >= 500 {
                let scheduled = mutable.lock().unwrap().stats.total_scheduled;
                let total_received = shared_state.get_statistics().total_received;
                // Use the known scenario total if available; fall back to total_received.
                let total_display = if cfg.simulation.total_requests > 0 {
                    cfg.simulation.total_requests
                } else {
                    total_received as usize
                };
                let pct = if total_display > 0 {
                    scheduled as f64 / total_display as f64 * 100.0
                } else {
                    0.0
                };
                print!(
                    "\r  [N={:2}] Scheduled {:>6}/{:<6} ({:5.1}%)  Received: {:>6}",
                    cfg.solver.batch_size, scheduled, total_display, pct, total_received
                );
                std::io::stdout().flush().ok();
                last_progress_wall_ms = wall_ms;
                printed_progress = true;
            }
        }
    }

    if printed_progress {
        // Do NOT emit a newline here: run_single_n will overwrite this line
        // with the definitive 100% final count using \r.
        use std::io::Write as _;
        std::io::stdout().flush().ok();
    }
}

// ─── batch dispatch ───────────────────────────────────────────────────────────

fn next_profile_to_dispatch(
    pending_by_profile: &[(QosProfileId, usize)],
    last_dispatched: Option<&QosProfileId>,
    active_profiles: &HashSet<QosProfileId>,
    allow_same_profile_parallelism: bool,
    last_infeasible: &HashMap<QosProfileId, (i32, usize)>,
    slot: i32,
    min_pending: usize,
) -> Option<(QosProfileId, usize)> {
    // When enabled, same-profile batches solve concurrently from independent
    // snapshots. Otherwise, an active profile is serialized. Only an
    // unchanged infeasible queue is held back.
    // Sorting before applying the cursor makes round-robin order deterministic.
    let mut eligible: Vec<_> = pending_by_profile
        .iter()
        .filter(|(profile_id, count)| {
            *count >= min_pending
                && (allow_same_profile_parallelism || !active_profiles.contains(profile_id))
                && last_infeasible.get(profile_id) != Some(&(slot, *count))
        })
        .map(|(profile_id, count)| (profile_id.clone(), *count))
        .collect();
    eligible.sort_by(|left, right| left.0.cmp(&right.0));
    if eligible.is_empty() {
        return None;
    }
    let next_index = last_dispatched
        .and_then(|cursor| {
            eligible
                .iter()
                .position(|(profile_id, _)| profile_id > cursor)
        })
        .unwrap_or(0);
    // Start after the last selected profile, wrapping to the first profile
    // when the cursor is at the end of the sorted eligible list.
    Some(eligible[next_index].clone())
}

/// Tell the slot-flush logic whether any queued profile can be retried now.
/// An unchanged infeasible queue is suppressed for this slot to avoid a hot
/// retry loop; a new slot or changed queue size makes it eligible again.
fn has_retryable_profile_pending(
    shared_state: &SharedState,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    slot: i32,
) -> bool {
    let pending_by_profile = shared_state.pending_profile_counts();
    let g = mutable.lock().unwrap();
    pending_by_profile.iter().any(|(profile_id, count)| {
        *count > 0 && g.last_infeasible.get(profile_id) != Some(&(slot, *count))
    })
}

/// Claim the next eligible batch while the caller owns the scheduler mutex.
fn claim_next_profile_batch(
    slot: i32,
    min_pending: usize,
    allow_same_profile_parallelism: bool,
    shared_state: &SharedState,
    cfg: &Config,
    scheduler_state: &mut SchedulerMutableState,
) -> Option<(QosProfileId, ClaimedRequestBatch)> {
    let (profile_id, pending_count) = next_profile_to_dispatch(
        &shared_state.pending_profile_counts(),
        scheduler_state.last_dispatched_profile.as_ref(),
        &scheduler_state.active_profiles,
        allow_same_profile_parallelism,
        &scheduler_state.last_infeasible,
        slot,
        min_pending,
    )?;
    let claimed = shared_state.claim_pending_requests_for_profile_with_snapshot(
        &profile_id,
        pending_count.min(cfg.solver.batch_size),
    );
    if claimed.requests.len() < min_pending {
        shared_state.requeue_pending_requests_front(claimed.requests);
        return None;
    }

    scheduler_state.last_dispatched_profile = Some(profile_id.clone());
    scheduler_state.active_profiles.insert(profile_id.clone());
    Some((profile_id, claimed))
}

/// Fill the configured worker pool with independent, profile-homogeneous work.
/// Each worker keeps its slot and immediately claims another eligible batch
/// when one is available, avoiding a thread teardown/spawn gap between batches.
fn dispatch_batch_workers(
    slot: i32,
    shared_state: &SharedState,
    cfg: &Arc<Config>,
    solver_registry: &Arc<SolverRegistry>,
    carbon_forecast: &Arc<RwLock<Vec<f64>>>,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &Arc<MetricsLogger>,
    running: &Arc<AtomicBool>,
    flush: bool,
    allow_same_profile_parallelism: bool,
    scheduler_thread: &std::thread::Thread,
) -> bool {
    let mut dispatched = false;
    let min_pending = if flush { 1 } else { cfg.solver.batch_size };
    // The common runtime snapshots and merges solver state under its own
    // short-lived lock, so the configured worker cap applies to every engine.
    let max_workers = cfg.solver.max_batch_solver_parallelism;

    loop {
        if !running.load(Ordering::Relaxed) {
            return dispatched;
        }

        let (profile_id, claimed) = {
            let mut g = mutable.lock().unwrap();
            if g.active_workers >= max_workers {
                return dispatched;
            }
            let Some(claimed) = claim_next_profile_batch(
                slot,
                min_pending,
                allow_same_profile_parallelism,
                shared_state,
                cfg,
                &mut g,
            ) else {
                return dispatched;
            };
            let now = Instant::now();
            let active_workers = g.active_workers;
            g.stats
                .record_worker_count_change(active_workers, active_workers + 1, now);
            g.active_workers += 1;
            g.stats.peak_concurrent_workers = g.stats.peak_concurrent_workers.max(g.active_workers);
            claimed
        };

        let shared_state = shared_state.clone();
        let cfg = cfg.clone();
        let solver_registry = Arc::clone(solver_registry);
        let carbon_forecast = carbon_forecast.clone();
        let mutable = mutable.clone();
        let ml = ml.clone();
        let running = running.clone();
        let scheduler_thread = scheduler_thread.clone();
        std::thread::spawn(move || {
            batch_worker_loop(
                slot,
                profile_id,
                claimed,
                min_pending,
                allow_same_profile_parallelism,
                shared_state,
                cfg,
                solver_registry,
                carbon_forecast,
                mutable,
                ml,
                running,
                scheduler_thread,
            );
        });
        dispatched = true;
    }
}

fn batch_worker_loop(
    mut slot: i32,
    mut profile_id: QosProfileId,
    mut claimed: ClaimedRequestBatch,
    min_pending: usize,
    allow_same_profile_parallelism: bool,
    shared_state: SharedState,
    cfg: Arc<Config>,
    solver_registry: Arc<SolverRegistry>,
    carbon_forecast: Arc<RwLock<Vec<f64>>>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    ml: Arc<MetricsLogger>,
    running: Arc<AtomicBool>,
    scheduler_thread: std::thread::Thread,
) {
    loop {
        let profile_pending_count = claimed.profile_pending_count;
        let forecast = carbon_forecast.read().unwrap().clone();
        let outcome = batch_worker_entry(
            slot,
            claimed.requests,
            claimed.snapshot,
            &shared_state,
            &cfg,
            &solver_registry,
            &forecast,
            &mutable,
            &ml,
        );

        let mut g = mutable.lock().unwrap();
        match outcome {
            BatchWorkerOutcome::Committed => {
                g.last_infeasible.remove(&profile_id);
            }
            BatchWorkerOutcome::Infeasible => {
                g.last_infeasible
                    .insert(profile_id.clone(), (slot, profile_pending_count));
            }
        }
        g.active_profiles.remove(&profile_id);

        let next_slot = shared_state.get_current_slot();
        let next_batch = if running.load(Ordering::Relaxed) {
            claim_next_profile_batch(
                next_slot,
                min_pending,
                allow_same_profile_parallelism,
                &shared_state,
                &cfg,
                &mut g,
            )
        } else {
            None
        };
        if let Some((next_profile_id, next_claimed)) = next_batch {
            profile_id = next_profile_id;
            claimed = next_claimed;
            slot = next_slot;
            drop(g);
            continue;
        }

        let now = Instant::now();
        let active_workers = g.active_workers;
        g.stats
            .record_worker_count_change(active_workers, active_workers - 1, now);
        g.active_workers -= 1;
        if !running.load(Ordering::Relaxed) && g.active_workers == 0 {
            g.stats.finish(0, now);
        }
        drop(g);
        scheduler_thread.unpark();
        return;
    }
}

// ─── worker entry ─────────────────────────────────────────────────────────────

/// Return the cached engine runtime for this profile, creating it on first use.
///
/// The runtime owns the profile's persistent Bandit/ACO learning state. We
/// return an `Arc` so a worker can keep using that runtime after the scheduler
/// state lock has been released.
fn get_runtime(
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    registry: &SolverRegistry,
    engine_name: &str,
    scope: SolverScope,
) -> Result<Arc<dyn SolverRuntime>, SolverError> {
    let profile_id = scope.profile_id.clone();

    // The usual case is a cache hit. Clone the Arc while locked, then return
    // an owned handle; never return a reference into the mutex-protected map.
    {
        let scheduler_state = mutable.lock().map_err(|_| SolverError::LockPoisoned)?;
        if let Some(runtime) = scheduler_state.solver_runtimes.get(&profile_id) {
            return Ok(Arc::clone(runtime));
        }
    }

    // Runtime initialization may allocate algorithm state proportional to the
    // horizon. Do that without holding the scheduler-wide mutex.
    let new_runtime: Arc<dyn SolverRuntime> = Arc::from(registry.create(engine_name, scope)?);

    // Another worker for this profile may have initialized the runtime while
    // this worker was creating its candidate. The second lookup ensures both
    // workers use the one runtime stored in the cache; the unused candidate
    // is simply dropped.
    let mut scheduler_state = mutable.lock().map_err(|_| SolverError::LockPoisoned)?;
    let runtime = scheduler_state
        .solver_runtimes
        .entry(profile_id)
        .or_insert(new_runtime);
    Ok(Arc::clone(runtime))
}

/// Solve and commit one already-claimed, single-profile batch using its claim-time snapshot.
/// Requires that pending requests share the same QoS profile.
fn batch_worker_entry(
    slot: i32,
    pending: Vec<Request>,
    snapshot: SolverSnapshot,
    shared_state: &SharedState,
    cfg: &Config,
    solver_registry: &SolverRegistry,
    carbon_forecast: &[f64],
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> BatchWorkerOutcome {
    let capacity_tiers_guard = shared_state.capacity_tiers_guard();
    let capacity_tiers = capacity_tiers_guard
        .as_deref()
        .unwrap_or(&cfg.capacity_tiers);

    let Some(first_request) = pending.first() else {
        eprintln!("Scheduler received an empty batch.");
        return BatchWorkerOutcome::Infeasible;
    };
    let profile_id = first_request.qos_profile_id();
    let qos_profile = first_request.qos_profile.clone();
    if pending
        .iter()
        .any(|request| request.qos_profile_id() != profile_id)
    {
        eprintln!("Scheduler received a batch containing multiple QoS profiles.");
        shared_state.requeue_pending_requests_front(pending);
        return BatchWorkerOutcome::Infeasible;
    }

    // Unprofiled callers are valid only for the built-in default profile.
    // Custom profile IDs must carry the immutable profile snapshot resolved
    // when the request was admitted.
    let mut assignment = match qos_profile.as_deref() {
        Some(profile) if profile.profile_id == profile_id => {
            cfg.assignment_policy_for_profile(profile)
        }
        Some(profile) => {
            eprintln!(
                "Request profile ID {profile_id} does not match resolved profile {}.",
                profile.profile_id
            );
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
        None if profile_id == QosProfileId::default_profile() => cfg.assignment_policy(),
        None => {
            eprintln!("Request profile {profile_id} has no resolved QoS definition.");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };
    assignment.capacity_tiers = capacity_tiers;

    let total_slots = match usize::try_from(cfg.total_slots) {
        Ok(total_slots) => total_slots,
        Err(error) => {
            eprintln!("Invalid solver planning horizon: {error}");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };

    let solve_input = match build_solve_input_from_snapshot(
        slot,
        &pending,
        &snapshot,
        &assignment,
        &cfg.simulation,
        &cfg.infeasibility,
        carbon_forecast,
    ) {
        Ok(input) => input,
        Err(error) => {
            eprintln!("Could not prepare solver input for profile {profile_id}: {error}");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };

    let scope = SolverScope {
        profile_id: profile_id.clone(),
        total_slots,
    };
    let runtime = match get_runtime(mutable, solver_registry, &cfg.solver.solver_strategy, scope) {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("Could not create solver runtime: {error}");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };

    if cfg.logging.verbose {
        println!(
            "[Scheduler] Worker start: slot={slot}, batch_size={}",
            pending.len()
        );
    }

    let t0 = Instant::now();

    // -------- STEP 1 : Solve - assignment proposal
    // No scheduler-state mutex is held while the engine solves.
    let pending_proposal = match runtime.propose(&solve_input) {
        Ok(proposal) => proposal,
        Err(error) => {
            eprintln!("Solver proposal failed: {error}");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };

    let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;

    // -------- STEP 2 : Price proposal and commit it to shared state
    // This is the assignment commit. It validates placements and prices them
    // against the live global occupancy, not the solver's estimated prices.
    let committed = match shared_state.materialize_and_commit_proposal(
        &pending_proposal.proposal,
        &pending,
        &snapshot,
        &profile_id,
        slot,
        assignment.flavours,
        carbon_forecast,
        assignment.capacity_tiers,
        assignment.carbon_cost_duration_scale,
    ) {
        Ok(assignments) => assignments,
        Err(error) => {
            if let Err(discard_error) = runtime.discard(pending_proposal.id) {
                eprintln!("Could not discard rejected proposal: {discard_error}");
            }
            eprintln!("Proposal was not committed: {error}");
            shared_state.requeue_pending_requests_front(pending);
            return BatchWorkerOutcome::Infeasible;
        }
    };

    // -------- STEP 2.1 : Inform solver of committed assignments
    // This updates solver learning from the final accepted prices.
    // It does not write assignments to SharedState.
    // This is fundamental for stateful solvers, where only committed
    // proposals influence the state
    let status = if let Err(error) = runtime.commit(
        pending_proposal.id,
        CommitReport {
            assignments: committed.clone(),
        },
    ) {
        // SharedState already committed. Do not requeue these requests.
        eprintln!("Assignments committed, but solver learning update failed: {error}");
        "committed_learning_update_failed"
    } else {
        "ok"
    };

    // The commit adapter rejects incomplete proposals, so every returned
    // assignment represents one newly claimed request.
    let assigned_count = committed.len();
    let total_cost: f64 = committed.iter().map(|a| a.carbon_cost).sum();

    let window_start = (slot - assignment.error_window_past).max(0);
    let window_end = (slot + assignment.error_window_future).min(assignment.total_slots - 1);
    let mut modeled_error_sum = solve_input.qos.error_window.baseline_err.sum;
    let mut modeled_request_count = solve_input.qos.error_window.baseline_err.count;
    for committed_assignment in &committed {
        if (window_start..=window_end).contains(&committed_assignment.scheduled_slot) {
            modeled_error_sum += committed_assignment.error;
            modeled_request_count += 1.0;
        }
    }
    let modeled_window_average = if modeled_request_count > 0.0 {
        modeled_error_sum / modeled_request_count
    } else {
        0.0
    };
    let solver_mode = runtime.engine_name().to_owned();
    let profile_error_constraint_active = solve_input.qos.cumulative_error_constraint_active;

    // Update scheduler counters and capture this run's unique sequence number.
    let run_sequence = {
        let mut g = mutable.lock().unwrap();
        g.stats.solver_runs += 1;
        g.stats.batches_processed += 1;
        g.stats.total_scheduled += assigned_count as u64;
        g.stats.solver_total_time_ms += elapsed_ms;
        g.stats.solver_total_requests += assigned_count as u64;
        g.stats.last_solver_elapsed_ms = elapsed_ms;
        g.stats.solver_runs
    };

    if cfg.logging.verbose {
        let avg_error: f64 =
            committed.iter().map(|a| a.error).sum::<f64>() / committed.len() as f64;
        println!(
            "[Scheduler] ✓ Scheduled {} new requests \
                         (cost={total_cost:.2}, error={avg_error:.2}%, solver={elapsed_ms:.2}ms)",
            assigned_count
        );
    }

    // Build and emit metrics log row.
    if ml.enabled {
        // Only log the NEW assignments from this batch — not all existing
        // assignments.  The old code fetched get_current_assignments() here
        // (all-time O(N) entries) and iterated every batch, producing
        // O(N²) total rows and memory pressure.
        let assignment_rows = build_assignment_rows(&committed, slot);
        let real_avg = shared_state
            .get_profile_window_error_stats(
                &profile_id,
                slot,
                assignment.error_window_past,
                assignment.error_window_future,
                &HashSet::new(),
            )
            .average;

        let mut run_row: HashMap<String, String> = HashMap::new();
        run_row.insert("run_sequence".into(), run_sequence.to_string());
        run_row.insert("current_slot".into(), slot.to_string());
        run_row.insert("new_assignments".into(), assigned_count.to_string());
        run_row.insert("solver_status".into(), status.to_owned());
        run_row.insert("solver_mode".into(), solver_mode);
        run_row.insert("qos_profile_id".into(), profile_id.to_string());
        run_row.insert("solver_elapsed_ms".into(), elapsed_ms.to_string());
        run_row.insert("total_carbon_cost".into(), total_cost.to_string());
        run_row.insert(
            "error_window_avg_after".into(),
            modeled_window_average.to_string(),
        );
        run_row.insert("error_window_avg_after_real".into(), real_avg.to_string());
        run_row.insert("error_window_start_slot".into(), window_start.to_string());
        run_row.insert("error_window_end_slot".into(), window_end.to_string());
        run_row.insert(
            "error_window_threshold".into(),
            assignment.max_error_threshold.to_string(),
        );
        run_row.insert(
            "profile_error_constraint_active".into(),
            profile_error_constraint_active.to_string(),
        );

        ml.log_solver_run(&run_row, &assignment_rows);
    }
    BatchWorkerOutcome::Committed
}

/// Convert committed assignment records into the row shape expected by the
/// metrics logger. Sorting makes a run's output stable even though assignments
/// may have been assembled from different solver paths.
fn build_assignment_rows(
    assignments: &[Assignment],
    current_slot: i32,
) -> Vec<HashMap<String, String>> {
    let mut rows = Vec::with_capacity(assignments.len());
    let mut sorted = assignments.to_vec();
    sorted.sort_by_key(|a| (a.scheduled_slot, a.request_id));
    for a in &sorted {
        let mut row = HashMap::new();
        row.insert("current_slot".into(), current_slot.to_string());
        row.insert("request_id".into(), a.request_id.to_string());
        row.insert("qos_profile_id".into(), a.qos_profile_id.to_string());
        row.insert("scheduled_slot".into(), a.scheduled_slot.to_string());
        row.insert("flavour_name".into(), a.flavour_name.clone());
        row.insert(
            "flavour_duration_sec".into(),
            a.flavour_duration.to_string(),
        );
        row.insert("error".into(), a.error.to_string());
        row.insert("carbon_cost".into(), a.carbon_cost.to_string());
        row.insert(
            "arrival_slot".into(),
            a.arrival_slot.map(|v| v.to_string()).unwrap_or_default(),
        );
        row.insert(
            "deadline_slot".into(),
            a.deadline_slot.map(|v| v.to_string()).unwrap_or_default(),
        );
        rows.push(row);
    }
    rows
}
