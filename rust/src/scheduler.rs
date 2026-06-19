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
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rand::SeedableRng;
use rand_distr::{Distribution, Normal};

use crate::config::Config;
use crate::dp_solver::{DpSolver, ErrorWindowBaseline, MockPool, SolveBatchInput};
use crate::metrics_logger::MetricsLogger;
use crate::shared_state::{CommitOutcome, SharedState, SolverSnapshot};
use crate::types::{Assignment, Flavour, Request, RequestAssignment};

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

/// Persistent mock-pool for infeasibility recovery.
#[derive(Debug, Default)]
struct PersistentMockPool {
    slot: Option<i32>,
    mode: Option<String>,
    remaining: i32,
    error: f64,
}

#[derive(Debug, Clone)]
struct MockInfluenceState {
    base: f64,
    effective: f64,
    above_threshold_streak: i32,
    last_eval_slot: Option<i32>,
}

/// All mutable scheduler state shared between the main loop and workers.
struct SchedulerMutableState {
    active_workers: usize,
    /// Anti-storm guard: (slot, pending_count) of last infeasible batch.
    last_infeasible: Option<(i32, usize)>,
    stats: SchedulerStats,
    mock_pool: PersistentMockPool,
    mock_influence: MockInfluenceState,
}

// ─── public result type ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Default)]
pub struct SolveContext {
    pub status: String,
    pub mode: String,
    pub new_assignments: usize,
    pub total_assignments: usize,
    pub global_error_before: f64,
    pub global_error_count_before: u64,
    pub global_error_constraint_active: bool,
    pub modeled_window_avg_after: f64,
    pub window_start_slot: i32,
    pub window_end_slot: i32,
    pub mock_recovery_consumed: i32,
    pub recovery_mode: String,
    pub solver_elapsed_ms: f64,
}

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

// ─── Simple error baseline (internal) ────────────────────────────────────────

#[derive(Debug, Clone, Default)]
struct ErrorBaseline {
    error_sum: f64,
    request_count: f64,
    average_error: f64,
}

impl ErrorBaseline {
    fn new(error_sum: f64, request_count: f64) -> Self {
        let avg = if request_count > 0.0 { error_sum / request_count } else { 0.0 };
        Self { error_sum, request_count, average_error: avg }
    }
}

// ─── BatchScheduler ──────────────────────────────────────────────────────────

pub struct BatchScheduler {
    shared_state: SharedState,
    cfg: Arc<Config>,
    /// Pre-computed carbon intensity forecast for all slots (sinusoidal pattern).
    carbon_forecast: Arc<Vec<f64>>,
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
        carbon_forecast: Option<Vec<f64>>,
    ) -> Self {
        let carbon_forecast = Arc::new(
            carbon_forecast.unwrap_or_else(|| generate_carbon_forecast(&cfg)),
        );
        let flavour_duration_by_name: HashMap<String, i32> =
            cfg.flavours.iter().map(|f| (f.name.clone(), f.duration)).collect();
        let mock_influence_base = cfg.infeasibility_mock_influence.clamp(0.0, 1.0);

        Self {
            shared_state,
            cfg: cfg.clone(),
            carbon_forecast,
            flavour_duration_by_name: Arc::new(flavour_duration_by_name),
            running: Arc::new(AtomicBool::new(false)),
            mutable: Arc::new(Mutex::new(SchedulerMutableState {
                active_workers: 0,
                last_infeasible: None,
                stats: SchedulerStats::default(),
                mock_pool: PersistentMockPool::default(),
                mock_influence: MockInfluenceState {
                    base: mock_influence_base,
                    effective: mock_influence_base,
                    above_threshold_streak: 0,
                    last_eval_slot: None,
                },
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

        if cfg.verbose {
            println!(
                "[Scheduler] Started (batch_size={}, max_parallel={})",
                cfg.batch_size, cfg.max_batch_solver_parallelism
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
            if self.mutable.lock().unwrap().active_workers == 0
                || Instant::now() > deadline
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if self.cfg.verbose {
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
            max_batch_parallelism: self.cfg.max_batch_solver_parallelism,
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
    carbon_forecast: Arc<Vec<f64>>,
    fdb: Arc<HashMap<String, i32>>,
    mutable: Arc<Mutex<SchedulerMutableState>>,
    ml: Arc<MetricsLogger>,
) {
    let wall_start = Instant::now();
    let slot_ms = (cfg.effective_slot_duration_secs() * 1000.0) as u64;
    let eff_slot_dur = cfg.effective_slot_duration_secs();
    let mut last_flush_slot: i32 = -1;
    let mut last_skip_slot: i32 = -1;

    while running.load(Ordering::Relaxed) {
        // Keep virtual clock in sync with wall clock (skip mode may advance it further).
        let wall_ms = wall_start.elapsed().as_millis() as u64;
        let current_vms = shared_state.virtual_elapsed_ms.load(Ordering::Relaxed);
        if wall_ms > current_vms {
            shared_state.set_virtual_elapsed_ms(wall_ms);
        }

        let elapsed = shared_state.virtual_elapsed_secs();
        let current_slot = (elapsed / eff_slot_dur) as i32;
        shared_state.set_current_slot(current_slot);

        let pending_count = shared_state.get_pending_count();
        let active_workers = mutable.lock().unwrap().active_workers;

        if pending_count >= cfg.batch_size && active_workers < cfg.max_batch_solver_parallelism {
            if cfg.verbose {
                println!(
                    "\n[Scheduler] Slot {current_slot}: {pending_count} pending, \
                     active_workers={active_workers}/{}",
                    cfg.max_batch_solver_parallelism
                );
            }
            dispatch_batch_workers(
                current_slot,
                &shared_state,
                &cfg,
                &carbon_forecast,
                &fdb,
                &mutable,
                &ml,
                &running,
                false,
            );
        } else if pending_count > 0
            && active_workers == 0
            && current_slot > last_flush_slot
        {
            // Partial-batch flush: requests are stranded (< batch_size) and no
            // workers are running.
            if cfg.verbose {
                println!(
                    "[Scheduler] Flush {pending_count} stale pending (slot={current_slot})"
                );
            }
            dispatch_batch_workers(
                current_slot,
                &shared_state,
                &cfg,
                &carbon_forecast,
                &fdb,
                &mutable,
                &ml,
                &running,
                true,
            );
            last_flush_slot = current_slot;
        } else if cfg.skip_empty_slots
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
            if cfg.verbose {
                println!("[Scheduler] ⏩ Skip slot {current_slot} → {}", current_slot + 1);
            }
        }

        std::thread::sleep(Duration::from_millis(10));
    }
}

// ─── batch dispatch ───────────────────────────────────────────────────────────

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
) {
    // In flush mode dispatch even a partial (< batch_size) batch; in normal
    // mode require a full batch so we amortise solver overhead.
    let min_pending = if flush { 1 } else { cfg.batch_size };

    loop {
        if !running.load(Ordering::Relaxed) {
            return;
        }

        let pending_count = shared_state.get_pending_count();
        let (active_workers, last_infeasible) = {
            let g = mutable.lock().unwrap();
            (g.active_workers, g.last_infeasible)
        };

        if pending_count < min_pending || active_workers >= cfg.max_batch_solver_parallelism {
            return;
        }

        // Anti-storm guard: same slot + same pending count → already infeasible.
        if last_infeasible == Some((slot, pending_count)) {
            return;
        }

        let claim_n = pending_count.min(cfg.batch_size);
        let pending = shared_state.claim_pending_requests(claim_n);
        if pending.is_empty() {
            return;
        }
        if pending.len() < min_pending {
            shared_state.requeue_pending_requests_front(pending);
            return;
        }

        // Increment active-worker counter before spawning so the main loop sees it.
        {
            let mut g = mutable.lock().unwrap();
            let new_count = g.active_workers + 1;
            g.active_workers = new_count;
            // Track peak and sum-for-average.
            if new_count > g.stats.peak_concurrent_workers {
                g.stats.peak_concurrent_workers = new_count;
            }
            g.stats.sum_active_workers_at_dispatch += new_count as u64;
        }

        let pending_len = pending.len();
        let ss = shared_state.clone();
        let cfg2 = cfg.clone();
        let forecast = carbon_forecast.clone();
        let fdb2 = fdb.clone();
        let mut2 = mutable.clone();
        let ml2 = ml.clone();

        std::thread::spawn(move || {
            let scheduled = batch_worker_entry(slot, pending, &ss, &cfg2, &forecast, &fdb2, &mut2, &ml2);
            let mut g = mut2.lock().unwrap();
            g.active_workers -= 1;
            if scheduled {
                g.last_infeasible = None;
            } else {
                g.last_infeasible = Some((slot, pending_len));
            }
        });
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
    if cfg.verbose {
        println!("[Scheduler] Worker start: slot={slot}, batch_size={}", pending.len());
    }

    let mut consecutive_rollbacks: usize = 0;

    loop {
        let t0 = Instant::now();
        let wall_start = unix_now_f64();

        let (assignments, ctx, baseline_slot_counts) =
            solve_dp(slot, &pending, shared_state, cfg, carbon_forecast, fdb, mutable, ml);

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
        let force_commit = cfg.rollback_max_consecutive == 0
            || consecutive_rollbacks >= cfg.rollback_max_consecutive;

        let outcome = shared_state.try_add_assignments_checked(
            &assignments,
            &expected_per_slot,
            &cfg.capacity_tiers,
            force_commit,
            consecutive_rollbacks,
        );

        match outcome {
            CommitOutcome::RolledBack => {
                consecutive_rollbacks += 1;
                if cfg.verbose {
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

                shared_state.export_to_csv(&cfg.output_file).ok();

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
                    (g.stats.solver_runs, g.stats.batches_processed, g.stats.total_scheduled)
                };

                if cfg.verbose {
                    let avg_error: f64 = assignments.iter().map(|a| a.error).sum::<f64>()
                        / assignments.len() as f64;
                    let rollback_note = if consecutive_rollbacks > 0 {
                        format!(" [after {consecutive_rollbacks} rollback(s)]")
                    } else {
                        String::new()
                    };
                    println!(
                        "[Scheduler] ✓ Scheduled {} new requests{}{} \
                         (cost={total_cost:.2}, error={avg_error:.2}%, solver={elapsed_ms:.2}ms)",
                        new_count,
                        if replanned > 0 { format!(" + {replanned} re-planned") } else { String::new() },
                        rollback_note,
                    );
                }

                // Build and emit metrics log row.
                if ml.enabled {
                    let new_ids: HashSet<u64> = pending.iter().map(|r| r.id).collect();
                    let pending_ids_str: HashSet<u64> = new_ids.clone();
                    let all_assignments = shared_state.get_current_assignments();

                    let real_window_after = shared_state.get_window_error_stats(
                        slot,
                        cfg.error_window_past,
                        cfg.error_window_future,
                        &HashSet::new(),
                    );

                    let assignment_rows = build_assignment_rows(
                        &all_assignments.values().cloned().collect::<Vec<_>>(),
                        &new_ids,
                        &pending_ids_str,
                        slot,
                        wall_start,
                        wall_end,
                    );

                    let avg_ms_per_new = if new_count > 0 { elapsed_ms / new_count as f64 } else { 0.0 };
                    let avg_ms_per_total = if total_count > 0 { elapsed_ms / total_count as f64 } else { 0.0 };
                    let avg_cost_per_new = if new_count > 0 { total_cost / new_count as f64 } else { 0.0 };
                    let avg_cost_per_total = if total_count > 0 { total_cost / total_count as f64 } else { 0.0 };
                    let modeled_avg = ctx.modeled_window_avg_after;
                    let real_avg = real_window_after.average;

                    let mut run_row: HashMap<String, String> = HashMap::new();
                    run_row.insert("run_sequence".into(), run_sequence.to_string());
                    run_row.insert("current_slot".into(), slot.to_string());
                    run_row.insert("pending_batch_size".into(), new_count.to_string());
                    run_row.insert("total_assignments".into(), total_count.to_string());
                    run_row.insert("new_assignments".into(), new_count.to_string());
                    run_row.insert("replanned_assignments".into(), replanned.to_string());
                    run_row.insert("solver_status".into(), ctx.status.clone());
                    run_row.insert("solver_mode".into(), ctx.mode.clone());
                    run_row.insert("consecutive_rollbacks".into(), consecutive_rollbacks.to_string());
                    run_row.insert("lock_future_assignments".into(), cfg.dp_lock_future_assignments.to_string());
                    run_row.insert("solver_start_ts".into(), wall_start.to_string());
                    run_row.insert("solver_end_ts".into(), wall_end.to_string());
                    run_row.insert("solver_elapsed_ms".into(), elapsed_ms.to_string());
                    run_row.insert("avg_ms_per_new_request".into(), avg_ms_per_new.to_string());
                    run_row.insert("avg_ms_per_assignment".into(), avg_ms_per_total.to_string());
                    run_row.insert("total_carbon_cost".into(), total_cost.to_string());
                    run_row.insert("carbon_cost_per_new_request".into(), avg_cost_per_new.to_string());
                    run_row.insert("carbon_cost_per_assignment".into(), avg_cost_per_total.to_string());
                    run_row.insert("error_window_avg_after".into(), modeled_avg.to_string());
                    run_row.insert("error_window_avg_after_real".into(), real_avg.to_string());
                    run_row.insert("error_window_start_slot".into(), ctx.window_start_slot.to_string());
                    run_row.insert("error_window_end_slot".into(), ctx.window_end_slot.to_string());
                    run_row.insert("error_window_threshold".into(), cfg.max_error_threshold.to_string());
                    run_row.insert(
                        "error_window_violated_after".into(),
                        (modeled_avg > cfg.max_error_threshold).to_string(),
                    );
                    run_row.insert(
                        "error_window_violated_after_real".into(),
                        (real_avg > cfg.max_error_threshold).to_string(),
                    );
                    run_row.insert("batches_processed_after".into(), batches_processed.to_string());
                    run_row.insert("total_scheduled_after".into(), total_scheduled.to_string());
                    run_row.insert("global_error_before".into(), ctx.global_error_before.to_string());
                    run_row.insert("global_error_count_before".into(), ctx.global_error_count_before.to_string());
                    run_row.insert(
                        "global_error_constraint_active".into(),
                        ctx.global_error_constraint_active.to_string(),
                    );

                    ml.log_solver_run(&run_row, &assignment_rows, &[]);
                }

                return true;
            }
        }
    }
}

// ─── core DP pipeline ─────────────────────────────────────────────────────────

fn solve_dp(
    current_slot: i32,
    pending: &[Request],
    shared_state: &SharedState,
    cfg: &Config,
    carbon_forecast: &[f64],
    fdb: &HashMap<String, i32>,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
    _ml: &MetricsLogger,
) -> (Vec<Assignment>, SolveContext, HashMap<i32, i32>) {
    let pending_ids: HashSet<u64> = pending.iter().map(|r| r.id).collect();

    // Deadline cap = end of error window.
    let window_start = (current_slot - cfg.error_window_past).max(0);
    let window_end = (current_slot + cfg.error_window_future).min(cfg.total_slots - 1);
    let assignment_cap = window_end;

    let cap_deadline = |d: i32| -> i32 {
        d.max(current_slot).min(assignment_cap).min(cfg.total_slots - 1)
    };

    // ── Step 1 (pre-solve): capture a consistent snapshot of shared state ────
    // All reads from shared state happen here, under a single lock acquisition.
    // The lock is released before the DP optimiser runs.
    let snapshot = shared_state.snapshot_for_solver();

    // ── Step 2: time-shifting ────────────────────────────────────────────────
    // If DP_LOCK_FUTURE_ASSIGNMENTS=True, future assignments are pinned as
    // baseline load.  If False, they join the DP pool for joint re-planning.
    let future_assignments = snapshot.get_future_assignments(current_slot);

    let mut dp_requests: Vec<(u64, i32)> = pending
        .iter()
        .map(|r| (r.id, cap_deadline(r.deadline_slot)))
        .collect();

    // Metadata for converting RequestAssignment → Assignment later.
    let mut assignment_metadata: HashMap<u64, (i32, i32)> = pending
        .iter()
        .map(|r| (r.id, (r.arrival_slot, r.deadline_slot)))
        .collect();

    let mut fixed_future: Vec<Assignment> = Vec::new();
    let mut movable_future_ids: HashSet<u64> = HashSet::new();

    if cfg.dp_lock_future_assignments {
        fixed_future = future_assignments.clone();
    } else {
        movable_future_ids = future_assignments.iter().map(|a| a.request_id).collect();
        for a in &future_assignments {
            let deadline = a.deadline_slot.unwrap_or_else(|| a.scheduled_slot.max(current_slot));
            let capped = cap_deadline(deadline);
            dp_requests.push((a.request_id, capped));
            assignment_metadata.insert(
                a.request_id,
                (a.arrival_slot.unwrap_or(0), capped),
            );
        }
    }

    // Baseline counts/durations from pinned future assignments.
    let mut baseline_slot_counts: HashMap<i32, i32> = HashMap::new();
    let mut baseline_slot_durations: HashMap<i32, i32> = HashMap::new();
    for a in &fixed_future {
        let s = a.scheduled_slot;
        *baseline_slot_counts.entry(s).or_insert(0) += 1;
        let dur = a.flavour_duration.max(fdb.get(&a.flavour_name).copied().unwrap_or(0));
        *baseline_slot_durations.entry(s).or_insert(0) += dur;
    }

    // ── Step 3: error baseline ──────────────────────────────────────────────
    // 3a. Real window error from snapshot.
    let ws = snapshot.get_window_error_stats(
        current_slot,
        cfg.error_window_past,
        cfg.error_window_future,
        &movable_future_ids,
    );
    let mut error_baseline = ErrorBaseline::new(ws.error_sum, ws.count as f64);

    // 3b. Decayed past extension (no-op when decay_slots == 0, which is the default).
    error_baseline = augment_with_decayed_past(
        current_slot,
        error_baseline,
        cfg,
        &snapshot,
        &movable_future_ids,
    );

    // 3c. Virtual prehistory for startup slots (disabled by default).
    error_baseline = augment_with_virtual_prehistory(current_slot, error_baseline, cfg);

    // 3d. Infeasibility recovery: inject mock low-error requests to dilute the baseline.
    let (augmented_baseline, mock_pool_input) =
        apply_infeasibility_recovery(current_slot, error_baseline.clone(), cfg, &snapshot, mutable);
    let error_baseline = augmented_baseline;

    // ── Step 4: global error constraint ────────────────────────────────────
    let global_stats = snapshot.get_global_error_stats();
    let mut solver_flavours = cfg.flavours.clone();
    let global_constraint_active;

    if cfg.global_error_constraint_enabled
        && global_stats.count > 0
        && global_stats.avg > cfg.max_error_threshold
    {
        global_constraint_active = true;
        if cfg.global_error_constraint_hard {
            let before = solver_flavours.len();
            solver_flavours.retain(|f| f.error <= cfg.max_error_threshold);
            if solver_flavours.is_empty() {
                // Safety: never remove all flavours.
                solver_flavours = cfg.flavours.clone();
            } else if cfg.verbose && solver_flavours.len() < before {
                println!(
                    "[Scheduler] ⚠ Global error constraint (HARD): \
                     global_avg={:.4}% > {:.2}% → {} flavours remaining",
                    global_stats.avg,
                    cfg.max_error_threshold,
                    solver_flavours.len()
                );
            }
        }
    } else {
        global_constraint_active = false;
    }

    // ── Step 5: DP solve ────────────────────────────────────────────────────
    let effective_pruning = get_effective_pruning_mode(pending.len(), cfg);
    let solver = build_solver(&solver_flavours, carbon_forecast, cfg, &effective_pruning);

    let base_counts_arr: Vec<i32> = (0..cfg.total_slots)
        .map(|s| baseline_slot_counts.get(&s).copied().unwrap_or(0))
        .collect();
    let base_durations_arr: Vec<i32> = (0..cfg.total_slots)
        .map(|s| baseline_slot_durations.get(&s).copied().unwrap_or(0))
        .collect();

    let dp_result = solver.solve_batch(SolveBatchInput {
        requests: &dp_requests,
        current_slot,
        capacity_multiplier: 1.0,
        capacity_tiers: &cfg.capacity_tiers,
        baseline_slot_counts: &baseline_slot_counts,
        baseline_slot_durations: &baseline_slot_durations,
        error_window_baseline: ErrorWindowBaseline {
            error_sum: error_baseline.error_sum,
            request_count: error_baseline.request_count,
        },
        max_error_threshold: Some(cfg.max_error_threshold),
        error_window_past: cfg.error_window_past,
        error_window_future: cfg.error_window_future,
        assignment_max_slot: Some(assignment_cap),
        dynamic_mock_pool: mock_pool_input.clone(),
    });

    let scheduled_pending_ids: HashSet<u64> = dp_result
        .iter()
        .filter(|a| pending_ids.contains(&a.request_id))
        .map(|a| a.request_id)
        .collect();

    let mut dp_assignments = dp_result;
    let mut solve_status = "ok".to_string();
    let mut solve_mode = format!("dp_{effective_pruning}");

    if scheduled_pending_ids.len() != pending_ids.len() {
        if cfg.verbose {
            println!(
                "[Scheduler] ⚠ Infeasible ({}/{} pending covered): trying relaxed retry.",
                scheduled_pending_ids.len(),
                pending_ids.len()
            );
        }

        // 5a. Relaxed retry: re-run without the error-window threshold.
        let (relaxed, relaxed_mode) = solve_relaxed_retry(
            current_slot,
            &dp_requests,
            cfg,
            carbon_forecast,
            &baseline_slot_counts,
            &baseline_slot_durations,
            &error_baseline,
            assignment_cap,
            &mock_pool_input,
            &solver_flavours,
        );

        let relaxed_pending: HashSet<u64> = relaxed
            .iter()
            .filter(|a| pending_ids.contains(&a.request_id))
            .map(|a| a.request_id)
            .collect();

        if relaxed_pending.len() == pending_ids.len() {
            dp_assignments = relaxed;
            solve_status = "ok_relaxed".to_string();
            solve_mode = relaxed_mode;
        } else {
            // 5b. Greedy fallback: schedule pending-only requests ignoring the
            //     error window completely.
            if cfg.verbose {
                println!("[Scheduler] ⚠ Still infeasible: forcing greedy scheduling.");
            }
            let pending_only: Vec<(u64, i32)> = pending
                .iter()
                .map(|r| (r.id, cap_deadline(r.deadline_slot)))
                .collect();
            let deadlines: Vec<i32> = pending_only.iter().map(|(_, d)| *d).collect();

            let greedy_solver = build_solver(&cfg.flavours, carbon_forecast, cfg, "none");
            let greedy = greedy_solver.greedy_fallback(
                &pending_only,
                &deadlines,
                current_slot,
                &cfg.capacity_tiers,
                &base_counts_arr,
                &base_durations_arr,
            );
            dp_assignments = greedy;
            solve_status = "ok_greedy_after_infeasible".to_string();
            solve_mode = "greedy_after_infeasible".to_string();
        }
    }

    // Safety check: if not all pending are covered, signal infeasibility.
    let final_pending_covered: usize = dp_assignments
        .iter()
        .filter(|a| pending_ids.contains(&a.request_id))
        .count();

    if final_pending_covered != pending_ids.len() {
        if cfg.verbose {
            println!("[Scheduler] ⚠ Infeasible batch; retrying later.");
        }
        return (
            vec![],
            SolveContext { status: "infeasible".to_string(), mode: solve_mode, ..Default::default() },
            HashMap::new(),
        );
    }

    // ── Step 6: convert RequestAssignment → Assignment ──────────────────────
    let assignments: Vec<Assignment> = dp_assignments
        .iter()
        .map(|dp_a| {
            let (arrival, deadline) =
                assignment_metadata.get(&dp_a.request_id).copied().unwrap_or((0, 0));
            let dur = fdb.get(&dp_a.flavour_name).copied().unwrap_or(0);
            Assignment::new(
                dp_a.request_id,
                dp_a.slot,
                dp_a.flavour_name.clone(),
                dp_a.carbon_cost,
                dp_a.error,
                dur,
                Some(arrival),
                Some(deadline),
            )
        })
        .collect();

    // Compute the modelled window average after this run (mirrors Python logic).
    let mut modeled_error_sum = error_baseline.error_sum;
    let mut modeled_count = error_baseline.request_count;
    let mut mock_remaining = mock_pool_input.initial_count;
    let mock_err = mock_pool_input.error_per_request;

    for a in &assignments {
        if a.scheduled_slot >= window_start && a.scheduled_slot <= window_end {
            modeled_error_sum += a.error;
            modeled_count += 1.0;
            if mock_remaining > 0 && mock_err > 0.0 {
                modeled_error_sum -= mock_err;
                modeled_count = (modeled_count - 1.0).max(0.0);
                mock_remaining -= 1;
            }
        }
    }
    let mock_consumed = (mock_pool_input.initial_count - mock_remaining).max(0);
    consume_mock_pool(current_slot, &solve_mode, mock_consumed, cfg, mutable);

    let ctx = SolveContext {
        status: solve_status,
        mode: solve_mode,
        new_assignments: pending_ids.len(),
        total_assignments: assignments.len(),
        global_error_before: global_stats.avg,
        global_error_count_before: global_stats.count,
        global_error_constraint_active: global_constraint_active,
        modeled_window_avg_after: if modeled_count > 0.0 {
            modeled_error_sum / modeled_count
        } else {
            0.0
        },
        window_start_slot: window_start,
        window_end_slot: window_end,
        mock_recovery_consumed: mock_consumed,
        recovery_mode: cfg.infeasibility_recovery_mode.clone(),
        solver_elapsed_ms: 0.0, // filled by the caller
    };

    (assignments, ctx, baseline_slot_counts)
}

// ─── error baseline augmentation helpers ─────────────────────────────────────

/// Augment the error baseline with linearly-decayed contributions from the
/// slots just outside the past window boundary.  No-op when
/// `error_window_past_decay_slots == 0` (the default).
fn augment_with_decayed_past(
    current_slot: i32,
    baseline: ErrorBaseline,
    cfg: &Config,
    snapshot: &SolverSnapshot,
    exclude: &HashSet<u64>,
) -> ErrorBaseline {
    let decay_slots = cfg.error_window_past_decay_slots.max(0) as usize;
    if decay_slots == 0 {
        return baseline;
    }

    let mut weighted_count = 0.0f64;
    let mut weighted_error_sum = 0.0f64;
    let denominator = (decay_slots + 1) as f64;

    for idx in 1..=decay_slots {
        let slot = current_slot - cfg.error_window_past - idx as i32;
        let slot_assignments: Vec<_> = snapshot
            .get_requests_in_slot(slot)
            .into_iter()
            .filter(|a| !exclude.contains(&a.request_id))
            .collect();
        let n = slot_assignments.len();
        if n == 0 {
            continue;
        }
        let slot_avg_err: f64 =
            slot_assignments.iter().map(|a| a.error).sum::<f64>() / n as f64;
        let weight = (decay_slots - idx + 1) as f64 / denominator;
        weighted_count += n as f64 * weight;
        weighted_error_sum += slot_avg_err * n as f64 * weight;
    }

    if weighted_count <= 0.0 {
        return baseline;
    }
    ErrorBaseline::new(
        baseline.error_sum + weighted_error_sum,
        baseline.request_count + weighted_count,
    )
}

/// Synthesise missing pre-history slots for startup iterations
/// (`current_slot < error_window_past`).  Disabled by default.
fn augment_with_virtual_prehistory(
    current_slot: i32,
    baseline: ErrorBaseline,
    cfg: &Config,
) -> ErrorBaseline {
    if !cfg.prehistory_use_virtual_past {
        return baseline;
    }
    let missing = (cfg.error_window_past - current_slot).max(0);
    if missing == 0 {
        return baseline;
    }

    let rate = cfg.predicted_requests_per_slot;
    let sigma = (rate * cfg.request_rate_std_factor).max(1.0);
    let virtual_avg_err = cfg.max_error_threshold * cfg.prehistory_error_ratio_of_threshold;

    let mut virtual_requests = 0i32;
    for offset in 0..missing {
        let seed = cfg
            .prehistory_random_seed
            .wrapping_add((current_slot as u64).wrapping_sub(missing as u64 + offset as u64));
        let count = if cfg.prehistory_stochastic_counts {
            let dist = Normal::new(rate, sigma).unwrap();
            let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
            (dist.sample(&mut rng) as i32).max(1)
        } else {
            rate.round().max(1.0) as i32
        };
        virtual_requests += count;
    }

    if virtual_requests <= 0 {
        return baseline;
    }
    ErrorBaseline::new(
        baseline.error_sum + virtual_requests as f64 * virtual_avg_err,
        baseline.request_count + virtual_requests as f64,
    )
}

/// Apply the configured infeasibility-recovery policy.
///
/// Returns (augmented_baseline, mock_pool_for_dp).
fn apply_infeasibility_recovery(
    current_slot: i32,
    baseline: ErrorBaseline,
    cfg: &Config,
    snapshot: &SolverSnapshot,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
) -> (ErrorBaseline, MockPool) {
    let mode = cfg.infeasibility_recovery_mode.trim().to_lowercase();

    // Update mock influence once per slot (needs the current baseline avg).
    update_mock_influence(current_slot, baseline.average_error, cfg, mutable);

    if mode == "min_error_recovery" {
        // No mock injection; reset any persistent pool.
        reset_mock_pool(mutable);
        return (baseline, MockPool::default());
    }

    // Retrieve (or seed) the persistent mock pool for this slot/mode.
    let (mock_count, mock_error, _source) =
        get_or_seed_mock_pool(current_slot, &mode, cfg, snapshot, mutable);

    if mock_count <= 0 || mock_error <= 0.0 {
        return (baseline, MockPool::default());
    }

    let augmented = ErrorBaseline::new(
        baseline.error_sum + mock_count as f64 * mock_error,
        baseline.request_count + mock_count as f64,
    );
    let pool = MockPool { initial_count: mock_count, error_per_request: mock_error };
    (augmented, pool)
}

// ─── mock pool helpers ────────────────────────────────────────────────────────

fn update_mock_influence(
    slot: i32,
    baseline_avg: f64,
    cfg: &Config,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
) {
    let mut g = mutable.lock().unwrap();
    if g.mock_influence.last_eval_slot == Some(slot) {
        return;
    }
    let base = cfg.infeasibility_mock_influence.clamp(0.0, 1.0);
    let decay = cfg.infeasibility_mock_influence_decay_step.max(0.0);
    g.mock_influence.base = base;
    if baseline_avg > cfg.max_error_threshold {
        g.mock_influence.above_threshold_streak += 1;
        g.mock_influence.effective =
            (base - g.mock_influence.above_threshold_streak as f64 * decay).max(0.0);
    } else {
        g.mock_influence.above_threshold_streak = 0;
        g.mock_influence.effective = base;
    }
    g.mock_influence.last_eval_slot = Some(slot);
}

/// Retrieve the persistent mock pool for `(slot, mode)`, seeding it if needed.
///
/// The seed computation (which may call `shared_state`) is done outside the
/// lock to avoid holding it during I/O.
fn get_or_seed_mock_pool(
    slot: i32,
    mode: &str,
    cfg: &Config,
    snapshot: &SolverSnapshot,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
) -> (i32, f64, &'static str) {
    // First lock: check if we already have this slot/mode cached.
    let (has_pool, remaining, error) = {
        let g = mutable.lock().unwrap();
        let same =
            g.mock_pool.slot == Some(slot) && g.mock_pool.mode.as_deref() == Some(mode);
        (same, g.mock_pool.remaining, g.mock_pool.error)
    };
    if has_pool {
        return (remaining, error, "persistent_remaining");
    }

    // Compute outside the lock (reads snapshot for carryover mode).
    let influence = {
        let g = mutable.lock().unwrap();
        g.mock_influence.effective
    };
    let (new_count, new_error) = compute_mock_seed(slot, mode, cfg, influence, snapshot);

    // Second lock: store the new values.
    let mut g = mutable.lock().unwrap();
    g.mock_pool.slot = Some(slot);
    g.mock_pool.mode = Some(mode.to_string());
    g.mock_pool.remaining = new_count.max(0);
    g.mock_pool.error = new_error.max(0.0);
    (g.mock_pool.remaining, g.mock_pool.error, "new_window_seed")
}

fn compute_mock_seed(
    slot: i32,
    mode: &str,
    cfg: &Config,
    influence: f64,
    snapshot: &SolverSnapshot,
) -> (i32, f64) {
    let (mut count, error) = match mode {
        "carryover_last_slot" => {
            let window_start = (slot - cfg.error_window_past).max(0);
            let dropped_slot = window_start - 1;
            if dropped_slot < 0 {
                return (0, 0.0);
            }
            let dropped = snapshot.get_requests_in_slot(dropped_slot);
            let n = dropped.len() as i32;
            if n == 0 {
                return (0, 0.0);
            }
            let avg_err = dropped.iter().map(|a| a.error).sum::<f64>() / n as f64;
            let mock_err = resolve_mock_error(avg_err, cfg);
            (n, mock_err)
        }
        "forecast_mock_current_slot" => {
            let rate = cfg.predicted_requests_per_slot;
            let sigma = (rate * cfg.request_rate_std_factor).max(1.0);
            let seed = cfg.prehistory_random_seed.wrapping_add(slot as u64);
            let dist = Normal::new(rate, sigma).unwrap();
            let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
            let n = (dist.sample(&mut rng) as i32).max(0);
            let default_err = cfg.max_error_threshold * cfg.forecast_error_ratio_of_threshold;
            (n, resolve_mock_error(default_err, cfg))
        }
        _ => return (0, 0.0),
    };

    if count > 0 {
        count = (count as f64 * influence).round() as i32;
    }
    (count.max(0), error.max(0.0))
}

fn resolve_mock_error(fallback: f64, cfg: &Config) -> f64 {
    match cfg.infeasibility_mock_error_per_request {
        Some(v) => v.max(0.0),
        None => fallback.max(0.0),
    }
}

fn consume_mock_pool(
    slot: i32,
    mode: &str,
    consumed: i32,
    cfg: &Config,
    mutable: &Arc<Mutex<SchedulerMutableState>>,
) {
    if cfg.infeasibility_recovery_mode.trim().to_lowercase() == "min_error_recovery" {
        return;
    }
    let mut g = mutable.lock().unwrap();
    if g.mock_pool.slot == Some(slot) && g.mock_pool.mode.as_deref() == Some(mode) {
        g.mock_pool.remaining = (g.mock_pool.remaining - consumed.max(0)).max(0);
    }
}

fn reset_mock_pool(mutable: &Arc<Mutex<SchedulerMutableState>>) {
    let mut g = mutable.lock().unwrap();
    g.mock_pool.slot = None;
    g.mock_pool.mode = None;
    g.mock_pool.remaining = 0;
    g.mock_pool.error = 0.0;
}

// ─── relaxed retry ────────────────────────────────────────────────────────────

/// Re-run DP without the strict error-window threshold.
///
/// Returns `(assignments, mode_label)`.  Empty vec when relaxed retry is
/// disabled or fails.
fn solve_relaxed_retry(
    current_slot: i32,
    dp_requests: &[(u64, i32)],
    cfg: &Config,
    carbon_forecast: &[f64],
    baseline_slot_counts: &HashMap<i32, i32>,
    baseline_slot_durations: &HashMap<i32, i32>,
    error_baseline: &ErrorBaseline,
    assignment_cap: i32,
    mock_pool: &MockPool,
    solver_flavours: &[Flavour],
) -> (Vec<RequestAssignment>, String) {
    if !cfg.dp_allow_relaxed_error_retry {
        return (vec![], "dp_relaxed_disabled".to_string());
    }

    let prefer_min = cfg.dp_relaxed_retry_prefer_min_error;
    let relaxed_flavours: Vec<Flavour> = if prefer_min && !solver_flavours.is_empty() {
        let min_err = solver_flavours
            .iter()
            .map(|f| f.error)
            .fold(f64::INFINITY, f64::min);
        let filtered: Vec<Flavour> = solver_flavours
            .iter()
            .filter(|f| (f.error - min_err).abs() < 1e-9)
            .cloned()
            .collect();
        if filtered.is_empty() { solver_flavours.to_vec() } else { filtered }
    } else {
        solver_flavours.to_vec()
    };

    let mode_label = if prefer_min {
        "dp_relaxed_min_error".to_string()
    } else {
        "dp_relaxed_error".to_string()
    };

    let solver = build_solver(&relaxed_flavours, carbon_forecast, cfg, "none");
    let result = solver.solve_batch(SolveBatchInput {
        requests: dp_requests,
        current_slot,
        capacity_multiplier: 1.0,
        capacity_tiers: &cfg.capacity_tiers,
        baseline_slot_counts,
        baseline_slot_durations,
        error_window_baseline: ErrorWindowBaseline {
            error_sum: error_baseline.error_sum,
            request_count: error_baseline.request_count,
        },
        max_error_threshold: None, // relaxed: no error constraint
        error_window_past: cfg.error_window_past,
        error_window_future: cfg.error_window_future,
        assignment_max_slot: Some(assignment_cap),
        dynamic_mock_pool: mock_pool.clone(),
    });

    (result, mode_label)
}

// ─── misc helpers ─────────────────────────────────────────────────────────────

fn get_effective_pruning_mode(batch_size: usize, cfg: &Config) -> String {
    let threshold = cfg.dp_pruning_min_batch_size;
    if threshold == 0 || batch_size < threshold {
        "none".to_string()
    } else {
        cfg.dp_pruning_method.trim().to_lowercase()
    }
}

fn build_solver(
    flavours: &[Flavour],
    carbon_forecast: &[f64],
    cfg: &Config,
    pruning: &str,
) -> DpSolver {
    DpSolver {
        flavours: flavours.to_vec(),
        carbon_forecast: carbon_forecast.to_vec(),
        window_size: cfg.total_slots,
        pruning: pruning.to_string(),
        pruning_k: cfg.dp_pruning_k,
        timeout: cfg.dp_timeout,
        carbon_cost_scale: cfg.carbon_cost_duration_scale,
    }
}

/// Generate a sinusoidal carbon-intensity forecast matching the Python scheduler.
///
/// Uses K=6 slots per cycle, base_carbon=250, amplitude=200 (as in Python's
/// `_get_carbon_forecast`).
pub fn generate_carbon_forecast(cfg: &Config) -> Vec<f64> {
    let k = 6.0f64;
    let base = 250.0f64;
    let amplitude = 200.0f64;
    (0..cfg.total_slots)
        .map(|slot| {
            let phase = 2.0 * std::f64::consts::PI * (slot as f64 % k) / k;
            let value = base + amplitude * (1.0 + 0.8 * phase.cos());
            value.max(100.0)
        })
        .collect()
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
        row.insert("is_pending_request".into(), pending_ids.contains(&a.request_id).to_string());
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

// ─── unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics_logger::MetricsLogger;
    use crate::shared_state::SharedState;
    use crate::types::{Assignment, Flavour, Request};

    // ── helpers ───────────────────────────────────────────────────────────────

    fn make_config(overrides: impl FnOnce(&mut Config)) -> Config {
        let mut cfg = Config {
            batch_size: 3,
            total_slots: 24,
            error_window_past: 4,
            error_window_future: 4,
            max_error_threshold: 4.0,
            dp_lock_future_assignments: true,
            dp_allow_relaxed_error_retry: false,
            infeasibility_recovery_mode: "min_error_recovery".to_string(),
            infeasibility_mock_influence: 0.0,
            verbose: false,
            enable_solver_logging: false,
            global_error_constraint_enabled: false,
            dp_pruning_min_batch_size: 0,
            dp_pruning_method: "none".to_string(),
            ..Config::default()
        };
        overrides(&mut cfg);
        cfg
    }

    fn make_mutable_state(cfg: &Config) -> Arc<Mutex<SchedulerMutableState>> {
        let base = cfg.infeasibility_mock_influence.clamp(0.0, 1.0);
        Arc::new(Mutex::new(SchedulerMutableState {
            active_workers: 0,
            last_infeasible: None,
            stats: SchedulerStats::default(),
            mock_pool: PersistentMockPool::default(),
            mock_influence: MockInfluenceState {
                base,
                effective: base,
                above_threshold_streak: 0,
                last_eval_slot: None,
            },
        }))
    }

    fn disabled_logger() -> MetricsLogger {
        MetricsLogger::new(false, String::new(), String::new(), String::new(), None)
    }

    fn flat_forecast(slots: i32, value: f64) -> Vec<f64> {
        vec![value; slots as usize]
    }

    fn req(id: u64, arrival: i32, deadline: i32) -> Request {
        Request { id, arrival_slot: arrival, arrival_time: 0.0, deadline_slot: deadline }
    }

    fn call_solve_dp(
        current_slot: i32,
        pending: &[Request],
        cfg: &Config,
    ) -> (Vec<Assignment>, SolveContext) {
        let ss = SharedState::new();
        ss.set_current_slot(current_slot);
        let forecast = flat_forecast(cfg.total_slots, 100.0);
        let fdb: HashMap<String, i32> =
            cfg.flavours.iter().map(|f| (f.name.clone(), f.duration)).collect();
        let mutable = make_mutable_state(cfg);
        let ml = disabled_logger();
        let (a, c, _) = solve_dp(current_slot, pending, &ss, cfg, &forecast, &fdb, &mutable, &ml);
        (a, c)
    }

    fn call_solve_dp_with_state(
        current_slot: i32,
        pending: &[Request],
        cfg: &Config,
        ss: &SharedState,
    ) -> (Vec<Assignment>, SolveContext) {
        ss.set_current_slot(current_slot);
        let forecast = flat_forecast(cfg.total_slots, 100.0);
        let fdb: HashMap<String, i32> =
            cfg.flavours.iter().map(|f| (f.name.clone(), f.duration)).collect();
        let mutable = make_mutable_state(cfg);
        let ml = disabled_logger();
        let (a, c, _) = solve_dp(current_slot, pending, ss, cfg, &forecast, &fdb, &mutable, &ml);
        (a, c)
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    /// All assigned slots must be ≥ current_slot.
    #[test]
    fn test_scheduled_slots_never_before_current_slot() {
        let cfg = make_config(|_| {});
        let current_slot = 5;
        let pending = vec![
            req(0, current_slot, current_slot + 3),
            req(1, current_slot, current_slot + 3),
            req(2, current_slot, current_slot + 4),
        ];

        let (assignments, _ctx) = call_solve_dp(current_slot, &pending, &cfg);

        assert!(!assignments.is_empty(), "all requests should be scheduled");
        for a in &assignments {
            assert!(
                a.scheduled_slot >= current_slot,
                "scheduled_slot {} < current_slot {}",
                a.scheduled_slot,
                current_slot
            );
        }
    }

    /// With `dp_lock_future_assignments=true`, pre-existing future assignments
    /// are pinned as baseline load and must NOT appear in the DP result.
    #[test]
    fn test_lock_future_pins_and_excludes_future_assignments() {
        let cfg = make_config(|c| c.dp_lock_future_assignments = true);
        let ss = SharedState::new();
        let current_slot = 2i32;

        // Pre-place a future assignment (slot 6 > current_slot 2).
        let future_id = 99u64;
        ss.add_assignments(vec![Assignment::new(
            future_id, 6, "Fast".to_string(), 1.0, 5.0, 10, Some(0), Some(8),
        )]);

        let pending = vec![req(0, current_slot, current_slot + 3), req(1, current_slot, current_slot + 3), req(2, current_slot, current_slot + 4)];
        let (assignments, _ctx) = call_solve_dp_with_state(current_slot, &pending, &cfg, &ss);

        let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
        assert!(
            !result_ids.contains(&future_id),
            "future assignment should not be re-planned when dp_lock_future_assignments=true"
        );
        for i in 0u64..3 {
            assert!(result_ids.contains(&i), "pending request {} missing", i);
        }
    }

    /// With `dp_lock_future_assignments=false`, future assignments ARE included
    /// in the DP pool for joint re-planning.
    #[test]
    fn test_unlock_future_includes_future_in_dp() {
        let cfg = make_config(|c| c.dp_lock_future_assignments = false);
        let ss = SharedState::new();
        let current_slot = 2i32;

        let future_id = 99u64;
        ss.add_assignments(vec![Assignment::new(
            future_id, 6, "Fast".to_string(), 1.0, 5.0, 10, Some(0), Some(8),
        )]);

        let pending = vec![req(0, current_slot, current_slot + 3), req(1, current_slot, current_slot + 3), req(2, current_slot, current_slot + 4)];
        let (assignments, _ctx) = call_solve_dp_with_state(current_slot, &pending, &cfg, &ss);

        let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
        assert!(
            result_ids.contains(&future_id),
            "future assignment should be re-planned when dp_lock_future_assignments=false"
        );
    }

    /// When all flavours have error > threshold, DP is infeasible.
    /// The greedy fallback must cover all requests and the status must reflect it.
    #[test]
    fn test_greedy_fallback_covers_all_requests() {
        let cfg = make_config(|c| {
            c.max_error_threshold = 0.0;
            c.flavours = vec![Flavour { name: "Only".to_string(), error: 1.0, duration: 10 }];
            c.dp_allow_relaxed_error_retry = false;
        });

        let current_slot = 0;
        let pending = vec![req(0, 0, 5), req(1, 0, 5), req(2, 0, 5)];
        let (assignments, ctx) = call_solve_dp(current_slot, &pending, &cfg);

        assert_eq!(assignments.len(), 3, "greedy fallback should cover all 3 requests");
        assert!(
            ctx.status.contains("greedy"),
            "expected greedy status, got: {}",
            ctx.status
        );
        for a in &assignments {
            assert!(a.scheduled_slot >= 0 && a.scheduled_slot <= 5);
        }
    }

    /// `get_effective_pruning_mode` returns "none" when batch_size < threshold
    /// and the configured method when batch_size >= threshold.
    #[test]
    fn test_pruning_threshold_gate() {
        let cfg = make_config(|c| {
            c.dp_pruning_min_batch_size = 5;
            c.dp_pruning_method = "beam".to_string();
        });
        assert_eq!(get_effective_pruning_mode(3, &cfg), "none");
        assert_eq!(get_effective_pruning_mode(5, &cfg), "beam");
        assert_eq!(get_effective_pruning_mode(10, &cfg), "beam");
    }

    /// `get_effective_pruning_mode` returns "none" when the threshold is 0
    /// (disabled), regardless of batch size.
    #[test]
    fn test_pruning_threshold_zero_means_never_prune() {
        let cfg = make_config(|c| {
            c.dp_pruning_min_batch_size = 0;
            c.dp_pruning_method = "beam".to_string();
        });
        assert_eq!(get_effective_pruning_mode(100, &cfg), "none");
    }

    /// All pending requests should appear in the result even when deadlines vary.
    #[test]
    fn test_all_pending_requests_are_scheduled() {
        let cfg = make_config(|_| {});
        let current_slot = 0;
        let pending = vec![
            req(10, 0, 2),
            req(11, 0, 6),
            req(12, 0, 10),
        ];
        let (assignments, _ctx) = call_solve_dp(current_slot, &pending, &cfg);

        let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
        for id in [10u64, 11, 12] {
            assert!(result_ids.contains(&id), "request {} not scheduled", id);
        }
    }
}
