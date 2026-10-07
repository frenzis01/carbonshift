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
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use std::sync::RwLock;

use crate::config::{AssignmentPolicy, Config, SolverConfig, SwarmConfig};
use crate::engine::qos::QosProfileId;
use crate::metrics_logger::MetricsLogger;
use crate::shared_state::{CommitOutcome, SharedState};
use crate::types::{Assignment, Request};

mod forecast;
mod recovery;
mod solve;
#[cfg(test)]
mod tests;

pub use forecast::{advance_to_next_slot, generate_carbon_intensity_forecast};
use recovery::RecoveryState;
pub use solve::SolveContext;
#[cfg(test)]
use solve::get_effective_pruning_mode;
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
    sum_active_workers_at_dispatch: u64,
}

/// Wraps the two selectable online-swarm concurrency backends (see
/// `Config::swarm.mode`): `Serialized` (from `online_swarm.rs`, each
/// worker mutates it while holding the scheduler mutex) or `Merge` (from
/// `online_swarmerge.rs`, workers solve lock-free against a clone and
/// additively merge their contribution back). Irrelevant when the scheduler
/// uses the DP solver (`Config::solver.solver_strategy == "dp"`).
enum SwarmBackend {
    Serialized(crate::online_swarm::OnlineSwarmState),
    Merge(crate::online_swarmerge::OnlineSwarmState),
}

impl SwarmBackend {
    fn from_config(
        solver: &SolverConfig,
        swarm: &SwarmConfig,
        assignment: &AssignmentPolicy<'_>,
        carbon_forecast: &Arc<RwLock<Vec<f64>>>,
    ) -> Self {
        if swarm.mode == "merge" {
            Self::Merge(crate::online_swarmerge::OnlineSwarmState::from_config(
                solver,
                swarm,
                assignment,
                carbon_forecast,
            ))
        } else {
            Self::Serialized(crate::online_swarm::OnlineSwarmState::from_config(
                solver,
                swarm,
                assignment,
                carbon_forecast,
            ))
        }
    }

    /// Strategy name for logging/diagnostics (mirrors the wrapped state's
    /// `name()`; not currently read anywhere but kept for parity/future use).
    #[allow(dead_code)]
    fn name(&self) -> &'static str {
        match self {
            Self::Serialized(s) => s.name(),
            Self::Merge(s) => s.name(),
        }
    }
}

/// All mutable scheduler state shared between the main loop and workers.
struct SchedulerMutableState {
    active_workers: usize,
    /// Revision of the global tiers used by the cached online-solvers.
    capacity_tier_revision: u64,
    /// Only one solve may be in flight for a profile so its next batch sees
    /// the previous batch's committed error-budget contribution.
    active_profiles: HashSet<QosProfileId>,
    /// Anti-storm guard scoped to a profile's pending queue.
    last_infeasible: HashMap<QosProfileId, (i32, usize)>,
    /// Round-robin cursor prevents a busy profile from monopolizing workers.
    last_dispatched_profile: Option<QosProfileId>,
    stats: SchedulerStats,
    recovery: Arc<RecoveryState>,
    /// Online solver state is independent per profile; different flavour
    /// sets must not train the same cost/pheromone state.
    swarm_states: HashMap<QosProfileId, SwarmBackend>,
}

// ─── public result type ──────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct SchedulerStatistics {
    pub batches_processed: u64,
    pub total_scheduled: u64,
    pub solver_runs: u64,
    pub last_solver_elapsed_ms: f64,
    pub avg_solver_ms_per_batch: f64,
    pub avg_solver_ms_per_request: f64,
    pub active_batch_workers: usize,
    pub max_batch_parallelism: usize,
    pub peak_concurrent_workers: usize,
    pub avg_concurrent_workers: f64,
}

// ─── BatchScheduler ──────────────────────────────────────────────────────────

pub struct BatchScheduler {
    shared_state: SharedState,
    cfg: Arc<Config>,
    /// Pre-computed carbon-intensity forecast for the planning horizon.
    carbon_forecast: Arc<RwLock<Vec<f64>>>,
    /// flavour_name → duration_seconds lookup (immutable after construction).
    flavour_duration_by_name: Arc<HashMap<String, i32>>,
    running: Arc<AtomicBool>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    pub metrics_logger: Arc<MetricsLogger>,
    main_thread: Option<JoinHandle<()>>,
}

impl BatchScheduler {
    pub fn new(
        shared_state: SharedState,
        cfg: Arc<Config>,
        metrics_logger: Arc<MetricsLogger>,
        carbon_forecast: Arc<RwLock<Vec<f64>>>,
    ) -> Self {
        // Offline schedulers have no REST AppState to initialize the runtime
        // policy. For the live service this is idempotent with AppState::new.
        shared_state.initialize_capacity_tiers(cfg.capacity_tiers.clone());
        let capacity_tier_revision = shared_state.capacity_tier_revision();
        let carbon_forecast = carbon_forecast;
        let flavour_duration_by_name: HashMap<String, i32> = cfg
            .flavours
            .iter()
            .map(|f| (f.name.clone(), f.duration))
            .collect();
        Self {
            shared_state,
            cfg: cfg.clone(),
            carbon_forecast,
            flavour_duration_by_name: Arc::new(flavour_duration_by_name),
            running: Arc::new(AtomicBool::new(false)),
            mutable: Arc::new(Mutex::new(SchedulerMutableState {
                active_workers: 0,
                capacity_tier_revision,
                active_profiles: HashSet::new(),
                last_infeasible: HashMap::new(),
                last_dispatched_profile: None,
                stats: SchedulerStats::default(),
                recovery: Arc::new(RecoveryState::new()),
                swarm_states: HashMap::new(),
            })),
            metrics_logger,
            main_thread: None,
        }
    }

    /// Start the scheduler main-loop thread.
    pub fn start(&mut self) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }

        let running = self.running.clone();
        let ss = self.shared_state.clone();
        let cfg = self.cfg.clone();
        let forecast = self.carbon_forecast.clone();
        let fdb = self.flavour_duration_by_name.clone();
        let mutable = self.mutable.clone();
        let ml = self.metrics_logger.clone();

        if cfg.logging.verbose {
            println!(
                "[Scheduler] Started (batch_size={}, max_parallel={})",
                cfg.solver.batch_size, cfg.solver.max_batch_solver_parallelism
            );
        }

        self.main_thread = Some(std::thread::spawn(move || {
            main_loop(running, ss, cfg, forecast, fdb, mutable, ml);
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
        if self.cfg.logging.verbose {
            let batches = self.mutable.lock().unwrap().stats.batches_processed;
            println!("[Scheduler] Stopped (processed {batches} batches)");
        }
    }

    pub fn get_statistics(&self) -> SchedulerStatistics {
        let m = self.mutable.lock().unwrap();
        let runs = m.stats.solver_runs;
        let time_ms = m.stats.solver_total_time_ms;
        let reqs = m.stats.solver_total_requests;
        let dispatches = m.stats.solver_runs; // one dispatch per solver run
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
            avg_concurrent_workers: if dispatches > 0 {
                m.stats.sum_active_workers_at_dispatch as f64 / dispatches as f64
            } else {
                0.0
            },
        }
    }

    /// Virtual elapsed time in seconds (shared with generator and monitor).
    pub fn shared_state_virtual_elapsed_secs(&self) -> f64 {
        self.shared_state.virtual_elapsed_secs()
    }
}

// ─── main loop ───────────────────────────────────────────────────────────────

fn main_loop(
    running: Arc<AtomicBool>,
    shared_state: SharedState,
    cfg: Arc<Config>,
    carbon_forecast: Arc<RwLock<Vec<f64>>>,
    fdb: Arc<HashMap<String, i32>>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    ml: Arc<MetricsLogger>,
) {
    let wall_start = Instant::now();
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
        // Freezing only at full saturation (the previous condition) has a gap:
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

        // unwrap carbon_forecast here so that we get a consistent snapshot for this batch.
        let cf = Arc::new(carbon_forecast.read().unwrap().clone());

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
                &cf,
                &fdb,
                &mutable,
                &ml,
                &running,
                false,
            );
        } else if pending_count > 0
            && active_workers < cfg.solver.max_batch_solver_parallelism
            && (current_slot > last_flush_slot || flush_backlog)
        {
            // Slot-end flush: requests are stranded (< batch_size) and the slot
            // has advanced.  Dispatch even a partial batch so requests don't
            // miss their deadline waiting for the N-th arrival.
            if cfg.logging.verbose {
                println!("[Scheduler] Flush {pending_count} stale pending (slot={current_slot})");
            }
            did_something = dispatch_batch_workers(
                current_slot,
                &shared_state,
                &cfg,
                &cf,
                &fdb,
                &mutable,
                &ml,
                &running,
                true,
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
                        &cf,
                        &fdb,
                        &mutable,
                        &ml,
                        &running,
                        true,
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

        // Sleep longer when truly idle; poll at 1ms when workers are running or
        // requests are pending so we dispatch as fast as the solver allows.
        let sleep_ms = if did_something || active_workers > 0 {
            1
        } else {
            10
        };
        std::thread::sleep(Duration::from_millis(sleep_ms));

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
    last_infeasible: &HashMap<QosProfileId, (i32, usize)>,
    slot: i32,
    min_pending: usize,
) -> Option<(QosProfileId, usize)> {
    let mut eligible: Vec<_> = pending_by_profile
        .iter()
        .filter(|(profile_id, count)| {
            *count >= min_pending
                && !active_profiles.contains(profile_id)
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
    Some(eligible[next_index].clone())
}

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

fn dispatch_batch_workers(
    slot: i32,
    shared_state: &SharedState,
    cfg: &Arc<Config>,
    carbon_forecast: &Arc<Vec<f64>>,
    fdb: &Arc<HashMap<String, i32>>,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &Arc<MetricsLogger>,
    running: &Arc<AtomicBool>,
    flush: bool,
) -> bool {
    let mut dispatched = false;
    let min_pending = if flush { 1 } else { cfg.solver.batch_size };
    // Serialized swarm solvers update their persistent state in place. Keep
    // those solves one-at-a-time so a rollback cannot occur after their
    // strategy state has already been mutated. Every worker also holds the
    // shared capacity-tier read guard through solve and commit.
    let max_workers =
        if is_online_swarm_strategy(&cfg.solver.solver_strategy) && cfg.swarm.mode != "merge" {
            1
        } else {
            cfg.solver.max_batch_solver_parallelism
        };

    loop {
        if !running.load(Ordering::Relaxed) {
            return dispatched;
        }

        let (profile_id, profile_pending_count, pending) = {
            let mut g = mutable.lock().unwrap();
            if g.active_workers >= max_workers {
                return dispatched;
            }
            let Some((profile_id, profile_pending_count)) = next_profile_to_dispatch(
                &shared_state.pending_profile_counts(),
                g.last_dispatched_profile.as_ref(),
                &g.active_profiles,
                &g.last_infeasible,
                slot,
                min_pending,
            ) else {
                return dispatched;
            };
            let claim_count = profile_pending_count.min(cfg.solver.batch_size);
            let pending = shared_state.claim_pending_requests_for_profile(&profile_id, claim_count);
            if pending.is_empty() {
                return dispatched;
            }
            if pending.len() < min_pending {
                shared_state.requeue_pending_requests_front(pending);
                return dispatched;
            }

            g.last_dispatched_profile = Some(profile_id.clone());
            g.active_profiles.insert(profile_id.clone());
            g.active_workers += 1;
            let new_count = g.active_workers;
            if new_count > g.stats.peak_concurrent_workers {
                g.stats.peak_concurrent_workers = new_count;
            }
            g.stats.sum_active_workers_at_dispatch += new_count as u64;
            (profile_id, profile_pending_count, pending)
        };

        if pending.is_empty() {
            return dispatched;
        }

        let ss = shared_state.clone();
        let cfg2 = cfg.clone();
        let forecast = carbon_forecast.clone();
        let fdb2 = fdb.clone();
        let mut2 = mutable.clone();
        let ml2 = ml.clone();

        std::thread::spawn(move || {
            let scheduled =
                batch_worker_entry(slot, pending, &ss, &cfg2, &forecast, &fdb2, &mut2, &ml2);
            let mut g = mut2.lock().unwrap();
            g.active_workers -= 1;
            g.active_profiles.remove(&profile_id);
            if scheduled {
                g.last_infeasible.remove(&profile_id);
            } else {
                g.last_infeasible
                    .insert(profile_id, (slot, profile_pending_count));
            }
        });
        dispatched = true;
    }
}

// ─── worker entry ─────────────────────────────────────────────────────────────

fn batch_worker_entry(
    slot: i32,
    pending: Vec<Request>,
    shared_state: &SharedState,
    cfg: &Config,
    carbon_forecast: &[f64],
    fdb: &HashMap<String, i32>,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> bool {
    // Keep a read guard for this whole worker: the capacity setter takes the
    // corresponding write lock, so a running batch finishes its solve and
    // commit with the same tier ladder before a replacement becomes visible.
    let capacity_tiers_guard = shared_state.capacity_tiers_guard();
    let capacity_tiers = capacity_tiers_guard
        .as_deref()
        .unwrap_or(&cfg.capacity_tiers);
    let capacity_tier_revision = shared_state.capacity_tier_revision();
    {
        let mut scheduler_state = mutable.lock().unwrap();
        if scheduler_state.capacity_tier_revision != capacity_tier_revision {
            // Online bandit/ACO estimates are measured in carbon cost. They
            // cannot be compared to new costs after a tier switch, so discard
            // those learned estimates when this worker first sees the new
            // ladder. The tier read guard ensures no setter can race this
            // reset, solve, or the eventual commit.
            scheduler_state.swarm_states.clear();
            scheduler_state.capacity_tier_revision = capacity_tier_revision;
        }
    }

    let profile_id = pending
        .first()
        .map(Request::qos_profile_id)
        .unwrap_or_else(crate::engine::qos::QosProfileId::default_profile);
    let qos_profile = pending
        .first()
        .and_then(|request| request.qos_profile.clone());
    let mut assignment = qos_profile
        .as_deref()
        .map(|profile| cfg.assignment_policy_for_profile(profile))
        .unwrap_or_else(|| cfg.assignment_policy());
    assignment.capacity_tiers = capacity_tiers;
    debug_assert!(
        pending
            .iter()
            .all(|request| request.qos_profile_id() == profile_id),
        "batch_worker_entry requires a homogeneous QoS profile batch"
    );

    // Fork: swarm strategies bypass the DP solver entirely.
    if is_online_swarm_strategy(&cfg.solver.solver_strategy) {
        return batch_worker_entry_swarm(
            slot,
            pending,
            shared_state,
            cfg,
            &assignment,
            &profile_id,
            carbon_forecast,
            mutable,
            ml,
        );
    }

    if cfg.logging.verbose {
        println!(
            "[Scheduler] Worker start: slot={slot}, batch_size={}",
            pending.len()
        );
    }

    let recovery_state = Arc::clone(&mutable.lock().unwrap().recovery);
    let batch_solver = select_batch_solver(&cfg.solver.solver_strategy);
    let mut consecutive_rollbacks: usize = 0;

    loop {
        let t0 = Instant::now();
        let wall_start = unix_now_f64();

        let solve_result = batch_solver.solve(BatchSolveContext {
            current_slot: slot,
            pending: &pending,
            shared_state,
            assignment: &assignment,
            solver_config: &cfg.solver,
            simulation_config: &cfg.simulation,
            recovery_config: &cfg.infeasibility,
            recovery_state: &recovery_state,
            verbose: cfg.logging.verbose,
            carbon_forecast,
            flavour_duration_by_name: fdb,
        });
        let assignments = solve_result.assignments;
        let ctx = solve_result.context;
        let baseline_slot_counts = solve_result.baseline_slot_counts;

        let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;
        let wall_end = unix_now_f64();

        if assignments.is_empty() {
            // Infeasible: return pending to the front of the queue.
            shared_state.requeue_pending_requests_front(pending);
            return false;
        }

        // Compute the per-slot counts the solver assumed (baseline + batch).
        let mut expected_per_slot = baseline_slot_counts.clone();
        for a in &assignments {
            *expected_per_slot.entry(a.scheduled_slot).or_insert(0) += 1;
        }

        // TODO verify if this is okay
        // Limit expected_per_slot to only include the slots to which we are assigning requests in this batch.
        expected_per_slot.retain(|slot, _| assignments.iter().any(|a| a.scheduled_slot == *slot));

        // Attempt atomic commit; check for concurrent capacity-tier breach.
        let force_commit = cfg.solver.rollback_max_consecutive == 0
            || consecutive_rollbacks >= cfg.solver.rollback_max_consecutive;

        let outcome = shared_state.try_add_assignments_checked(
            &assignments,
            &expected_per_slot,
            assignment.capacity_tiers,
            force_commit,
            consecutive_rollbacks,
        );

        match outcome {
            CommitOutcome::RolledBack => {
                consecutive_rollbacks += 1;
                if cfg.logging.verbose {
                    println!(
                        "[Scheduler] ↩ Rollback #{consecutive_rollbacks} for slot={slot} \
                         (unintended capacity-tier breach); re-solving...",
                    );
                }
                // Wait a bit before retrying to avoid hot loop if the state is very contended
                // Generate random backoff between 10-90 ms to reduce thundering herd risk if many workers are contending
                // TODO
                // let backoff_ms = 10 + rand::thread_rng().gen_range(0..80);
                // std::thread::sleep(Duration::from_millis(backoff_ms));

                // Re-run solve_dp with the same pending batch but fresh shared_state view.
                continue;
            }
            CommitOutcome::Committed => {
                // If this batch went through rollbacks, flag the committed requests.
                if consecutive_rollbacks > 0 {
                    let req_ids: Vec<u64> = pending.iter().map(|r| r.id).collect();
                    shared_state.mark_requests_rolled_back(&req_ids);
                }

                let new_count = pending.len();
                let total_count = assignments.len();
                let replanned = total_count.saturating_sub(new_count);
                let total_cost: f64 = assignments.iter().map(|a| a.carbon_cost).sum();

                // Update stats and get run_sequence for logging.
                let (run_sequence, batches_processed, total_scheduled) = {
                    let mut g = mutable.lock().unwrap();
                    g.stats.solver_runs += 1;
                    g.stats.batches_processed += 1;
                    g.stats.total_scheduled += new_count as u64;
                    g.stats.solver_total_time_ms += elapsed_ms;
                    g.stats.solver_total_requests += new_count as u64;
                    g.stats.last_solver_elapsed_ms = elapsed_ms;
                    (
                        g.stats.solver_runs,
                        g.stats.batches_processed,
                        g.stats.total_scheduled,
                    )
                };

                if cfg.logging.verbose {
                    let avg_error: f64 =
                        assignments.iter().map(|a| a.error).sum::<f64>() / assignments.len() as f64;
                    let rollback_note = if consecutive_rollbacks > 0 {
                        format!(" [after {consecutive_rollbacks} rollback(s)]")
                    } else {
                        String::new()
                    };
                    println!(
                        "[Scheduler] ✓ Scheduled {} new requests{}{} \
                         (cost={total_cost:.2}, error={avg_error:.2}%, solver={elapsed_ms:.2}ms)",
                        new_count,
                        if replanned > 0 {
                            format!(" + {replanned} re-planned")
                        } else {
                            String::new()
                        },
                        rollback_note,
                    );
                }

                // Build and emit metrics log row.
                let new_ids: HashSet<u64> = pending.iter().map(|r| r.id).collect();
                let pending_ids_str: HashSet<u64> = new_ids.clone();
                if ml.enabled {
                    // Only log the NEW assignments from this batch — not all existing
                    // assignments.  The old code fetched get_current_assignments() here
                    // (all-time O(N) entries) and iterated every batch, producing
                    // O(N²) total rows and memory pressure.
                    let assignment_rows = build_assignment_rows(
                        &assignments,
                        &new_ids,
                        &pending_ids_str,
                        slot,
                        wall_start,
                        wall_end,
                    );

                    let avg_ms_per_new = if new_count > 0 {
                        elapsed_ms / new_count as f64
                    } else {
                        0.0
                    };
                    let avg_ms_per_total = if total_count > 0 {
                        elapsed_ms / total_count as f64
                    } else {
                        0.0
                    };
                    let avg_cost_per_new = if new_count > 0 {
                        total_cost / new_count as f64
                    } else {
                        0.0
                    };
                    let avg_cost_per_total = if total_count > 0 {
                        total_cost / total_count as f64
                    } else {
                        0.0
                    };
                    let modeled_avg = ctx.modeled_window_avg_after;
                    let real_avg = shared_state
                        .get_profile_window_error_stats(
                            &profile_id,
                            slot,
                            assignment.error_window_past,
                            assignment.error_window_future,
                            &HashSet::new(),
                        )
                        .average;

                    // TODO: remove this debug print
                    println!("[Scheduler] Newly assigned IDs in this run: {:?}", new_ids);

                    let mut run_row: HashMap<String, String> = HashMap::new();
                    run_row.insert("run_sequence".into(), run_sequence.to_string());
                    run_row.insert("current_slot".into(), slot.to_string());
                    run_row.insert("pending_batch_size".into(), new_count.to_string());
                    run_row.insert("total_assignments".into(), total_count.to_string());
                    run_row.insert("new_assignments".into(), new_count.to_string());
                    run_row.insert("replanned_assignments".into(), replanned.to_string());
                    run_row.insert("solver_status".into(), ctx.status.clone());
                    run_row.insert("solver_mode".into(), ctx.mode.clone());
                    run_row.insert("qos_profile_id".into(), profile_id.to_string());
                    run_row.insert(
                        "consecutive_rollbacks".into(),
                        consecutive_rollbacks.to_string(),
                    );
                    run_row.insert(
                        "lock_future_assignments".into(),
                        cfg.solver.dp_lock_future_assignments.to_string(),
                    );
                    run_row.insert("solver_start_ts".into(), wall_start.to_string());
                    run_row.insert("solver_end_ts".into(), wall_end.to_string());
                    run_row.insert("solver_elapsed_ms".into(), elapsed_ms.to_string());
                    run_row.insert("avg_ms_per_new_request".into(), avg_ms_per_new.to_string());
                    run_row.insert("avg_ms_per_assignment".into(), avg_ms_per_total.to_string());
                    run_row.insert("total_carbon_cost".into(), total_cost.to_string());
                    run_row.insert(
                        "carbon_cost_per_new_request".into(),
                        avg_cost_per_new.to_string(),
                    );
                    run_row.insert(
                        "carbon_cost_per_assignment".into(),
                        avg_cost_per_total.to_string(),
                    );
                    run_row.insert("error_window_avg_after".into(), modeled_avg.to_string());
                    run_row.insert("error_window_avg_after_real".into(), real_avg.to_string());
                    run_row.insert(
                        "error_window_start_slot".into(),
                        ctx.window_start_slot.to_string(),
                    );
                    run_row.insert(
                        "error_window_end_slot".into(),
                        ctx.window_end_slot.to_string(),
                    );
                    run_row.insert(
                        "error_window_threshold".into(),
                        assignment.max_error_threshold.to_string(),
                    );
                    run_row.insert(
                        "error_window_violated_after".into(),
                        (modeled_avg > assignment.max_error_threshold).to_string(),
                    );
                    run_row.insert(
                        "error_window_violated_after_real".into(),
                        (real_avg > assignment.max_error_threshold).to_string(),
                    );
                    run_row.insert(
                        "batches_processed_after".into(),
                        batches_processed.to_string(),
                    );
                    run_row.insert("total_scheduled_after".into(), total_scheduled.to_string());
                    run_row.insert(
                        "global_error_before".into(),
                        ctx.global_error_before.to_string(),
                    );
                    run_row.insert(
                        "global_error_count_before".into(),
                        ctx.global_error_count_before.to_string(),
                    );
                    run_row.insert(
                        "profile_error_before".into(),
                        ctx.profile_error_before.to_string(),
                    );
                    run_row.insert(
                        "profile_error_count_before".into(),
                        ctx.profile_error_count_before.to_string(),
                    );
                    run_row.insert("global_error_constraint_active".into(), "false".to_string());
                    run_row.insert(
                        "profile_error_constraint_active".into(),
                        ctx.profile_error_constraint_active.to_string(),
                    );

                    ml.log_solver_run(&run_row, &assignment_rows, &[]);
                }

                return true;
            }
        }
    }
}

// ─── online swarm batch worker ────────────────────────────────────────────────

fn is_online_swarm_strategy(strategy: &str) -> bool {
    matches!(
        strategy.trim().to_ascii_lowercase().as_str(),
        "bandit" | "ant_colony"
    )
}

/// Executes one batch of requests using an online swarm strategy (bandit or
/// ACO). Dispatches to one of two concurrency-safe implementations based on
/// `Config::swarm.mode` (see `SwarmBackend`).
fn batch_worker_entry_swarm(
    slot: i32,
    pending: Vec<Request>,
    shared_state: &SharedState,
    cfg: &Config,
    assignment: &AssignmentPolicy<'_>,
    profile_id: &QosProfileId,
    carbon_forecast: &[f64],
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> bool {
    let is_merge = cfg.swarm.mode == "merge";
    if is_merge {
        batch_worker_entry_swarm_merge(
            slot,
            pending,
            shared_state,
            cfg,
            assignment,
            profile_id,
            carbon_forecast,
            mutable,
            ml,
        )
    } else {
        batch_worker_entry_swarm_serialized(
            slot,
            pending,
            shared_state,
            cfg,
            assignment,
            profile_id,
            carbon_forecast,
            mutable,
            ml,
        )
    }
}

/// Serialized backend: solves while holding the scheduler mutex for the
/// whole call, so concurrent swarm workers never race on the same state —
/// correct and reproducible, at the cost of limiting swarm batches to one in
/// flight at a time (irrelevant to DP batches, since `solver_strategy` is
/// global to the run: DP and swarm never execute concurrently).
fn batch_worker_entry_swarm_serialized(
    slot: i32,
    pending: Vec<Request>,
    shared_state: &SharedState,
    cfg: &Config,
    assignment: &AssignmentPolicy<'_>,
    profile_id: &QosProfileId,
    carbon_forecast: &[f64],
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> bool {
    let t0 = Instant::now();
    let ctx = shared_state.swarm_context_snapshot_for_profile(Some(profile_id));

    let mut assignments = {
        let mut g = mutable.lock().unwrap();
        let swarm_state = g.swarm_states.entry(profile_id.clone()).or_insert_with(|| {
            let forecast = Arc::new(RwLock::new(carbon_forecast.to_vec()));
            SwarmBackend::from_config(&cfg.solver, &cfg.swarm, assignment, &forecast)
        });
        let SwarmBackend::Serialized(swarm) = swarm_state else {
            unreachable!("batch_worker_entry_swarm dispatched Serialized mode");
        };
        swarm.solve_batch(&pending, slot, carbon_forecast, &ctx, assignment)
    };
    for assignment in &mut assignments {
        assignment.qos_profile_id = profile_id.clone();
    }

    let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;
    if assignments.is_empty() {
        shared_state.requeue_pending_requests_front(pending);
        return false;
    }

    if try_commit_swarm_batch(
        &assignments,
        &ctx.slot_count,
        shared_state,
        assignment.capacity_tiers,
        cfg.solver.rollback_max_consecutive == 0,
        0,
    ) == CommitOutcome::RolledBack
    {
        shared_state.requeue_pending_requests_front(pending);
        return false;
    }

    finish_swarm_batch(
        slot,
        &pending,
        assignments,
        elapsed_ms,
        &cfg.solver.solver_strategy,
        mutable,
        ml,
    )
}

/// Merge backend: clones the swarm state, solves lock-free against the
/// clone (preserving full parallelism, like DP batches), then additively
/// merges its own contribution back — see `online_swarmerge.rs` for why this
/// never discards concurrent workers' updates, unlike a plain overwrite.
fn batch_worker_entry_swarm_merge(
    slot: i32,
    pending: Vec<Request>,
    shared_state: &SharedState,
    cfg: &Config,
    assignment: &AssignmentPolicy<'_>,
    profile_id: &QosProfileId,
    carbon_forecast: &[f64],
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> bool {
    let t0 = Instant::now();
    let mut consecutive_rollbacks = 0;
    let assignments = loop {
        // Snapshot committed state and clone this profile's swarm state.
        let (swarm_snapshot, ctx) = {
            let mut g = mutable.lock().unwrap();
            let swarm_state = g.swarm_states.entry(profile_id.clone()).or_insert_with(|| {
                let forecast = Arc::new(RwLock::new(carbon_forecast.to_vec()));
                SwarmBackend::from_config(&cfg.solver, &cfg.swarm, assignment, &forecast)
            });
            let SwarmBackend::Merge(swarm) = swarm_state else {
                unreachable!("batch_worker_entry_swarm dispatched Merge mode");
            };
            (
                swarm.clone(),
                shared_state.swarm_context_snapshot_for_profile(Some(profile_id)),
            )
        };

        // Solve lock-free and keep the delta local until global-capacity commit.
        let (mut assignments, delta) =
            swarm_snapshot.solve_batch(&pending, slot, carbon_forecast, &ctx, assignment);
        if assignments.is_empty() {
            shared_state.requeue_pending_requests_front(pending);
            return false;
        }
        for assignment in &mut assignments {
            assignment.qos_profile_id = profile_id.clone();
        }

        let force_commit = cfg.solver.rollback_max_consecutive == 0
            || consecutive_rollbacks >= cfg.solver.rollback_max_consecutive;
        match try_commit_swarm_batch(
            &assignments,
            &ctx.slot_count,
            shared_state,
            assignment.capacity_tiers,
            force_commit,
            consecutive_rollbacks,
        ) {
            CommitOutcome::RolledBack => consecutive_rollbacks += 1,
            CommitOutcome::Committed => {
                // A rolled-back candidate must not train the online strategy.
                let mut g = mutable.lock().unwrap();
                if let Some(SwarmBackend::Merge(swarm)) = g.swarm_states.get_mut(profile_id) {
                    swarm.merge_delta(delta);
                }
                break assignments;
            }
        }
    };
    let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;

    finish_swarm_batch(
        slot,
        &pending,
        assignments,
        elapsed_ms,
        &cfg.solver.solver_strategy,
        mutable,
        ml,
    )
}

fn try_commit_swarm_batch(
    assignments: &[Assignment],
    baseline_slot_counts: &HashMap<i32, i32>,
    shared_state: &SharedState,
    capacity_tiers: &[crate::types::CapacityTier],
    force_commit: bool,
    consecutive_rollbacks: usize,
) -> CommitOutcome {
    let mut expected_per_slot = baseline_slot_counts.clone();
    for assignment in assignments {
        *expected_per_slot
            .entry(assignment.scheduled_slot)
            .or_default() += 1;
    }
    expected_per_slot.retain(|slot, _| assignments.iter().any(|a| a.scheduled_slot == *slot));

    shared_state.try_add_assignments_checked(
        assignments,
        &expected_per_slot,
        capacity_tiers,
        force_commit,
        consecutive_rollbacks,
    )
}

/// Shared tail after a profile-aware swarm commit: update stats and log metrics.
fn finish_swarm_batch(
    slot: i32,
    pending: &[Request],
    assignments: Vec<Assignment>,
    elapsed_ms: f64,
    solver_strategy: &str,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    ml: &MetricsLogger,
) -> bool {
    let new_count = assignments.len();

    // NOTE: active_workers is decremented by the outer dispatch_batch_workers closure —
    // do NOT touch it here to avoid a double-decrement that would underflow to usize::MAX.
    let run_sequence = {
        let mut g = mutable.lock().unwrap();
        g.stats.solver_runs += 1;
        g.stats.batches_processed += 1;
        g.stats.total_scheduled += new_count as u64;
        g.stats.solver_total_requests += new_count as u64;
        g.stats.solver_total_time_ms += elapsed_ms;
        g.stats.last_solver_elapsed_ms = elapsed_ms;
        g.stats.solver_runs
    };

    // Metrics logging — mark all batch requests as new assignments so
    // compute_per_request includes them (is_new_assignment_in_run=true).
    {
        let wall_ts = unix_now_f64();
        let new_ids: HashSet<u64> = pending.iter().map(|r| r.id).collect();
        let total_cost: f64 = assignments.iter().map(|a| a.carbon_cost).sum();
        let assignment_rows =
            build_assignment_rows(&assignments, &new_ids, &new_ids, slot, wall_ts, wall_ts);
        let mut run_row: HashMap<String, String> = HashMap::new();
        run_row.insert("run_sequence".into(), run_sequence.to_string());
        run_row.insert("current_slot".into(), slot.to_string());
        run_row.insert("pending_batch_size".into(), new_count.to_string());
        run_row.insert("new_assignments".into(), new_count.to_string());
        run_row.insert("total_assignments".into(), new_count.to_string());
        if let Some(assignment) = assignments.first() {
            run_row.insert(
                "qos_profile_id".into(),
                assignment.qos_profile_id.to_string(),
            );
        }
        run_row.insert("solver_elapsed_ms".into(), elapsed_ms.to_string());
        run_row.insert("total_carbon_cost".into(), total_cost.to_string());
        run_row.insert("solver_mode".into(), solver_strategy.to_string());
        run_row.insert("solver_status".into(), "ok".into());
        ml.log_solver_run(&run_row, &assignment_rows, &[]);
    }

    true
}

/// Build per-assignment CSV rows.
fn build_assignment_rows(
    assignments: &[Assignment],
    new_ids: &HashSet<u64>,
    pending_ids: &HashSet<u64>,
    current_slot: i32,
    solver_start_ts: f64,
    solver_end_ts: f64,
) -> Vec<HashMap<String, String>> {
    let mut rows = Vec::with_capacity(assignments.len());
    let mut sorted = assignments.to_vec();
    sorted.sort_by_key(|a| (a.scheduled_slot, a.request_id));
    for a in &sorted {
        let mut row = HashMap::new();
        row.insert("current_slot".into(), current_slot.to_string());
        row.insert("solver_start_ts".into(), solver_start_ts.to_string());
        row.insert("solver_end_ts".into(), solver_end_ts.to_string());
        row.insert("request_id".into(), a.request_id.to_string());
        row.insert("qos_profile_id".into(), a.qos_profile_id.to_string());
        row.insert(
            "is_pending_request".into(),
            pending_ids.contains(&a.request_id).to_string(),
        );
        row.insert(
            "is_new_assignment_in_run".into(),
            new_ids.contains(&a.request_id).to_string(),
        );
        row.insert("scheduled_slot".into(), a.scheduled_slot.to_string());
        row.insert("flavour_name".into(), a.flavour_name.clone());
        row.insert("flavour_duration".into(), a.flavour_duration.to_string());
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

fn unix_now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
