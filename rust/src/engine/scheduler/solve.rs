//! Batch preparation and solver execution for the scheduler.

use std::collections::{HashMap, HashSet};

use crate::config::{AssignmentPolicy, InfeasibilityConfig, SimulationConfig, SolverConfig};
use crate::dp_solver::{DpSolver, ErrorWindowBaseline, MockPool, SolveBatchInput};
use crate::shared_state::{GlobalErrorStats, SharedState};
use crate::types::{
    Assignment, CapacityTier, Flavour, Request, RequestAssignment, get_capacity_multiplier,
};

use super::recovery::{
    ErrorBaseline, apply_infeasibility_recovery, augment_with_decayed_past,
    augment_with_virtual_prehistory, consume_mock_pool,
};

use super::recovery::RecoveryState;

/// Shared output metadata produced by one batch scheduling strategy.
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

/// The stable input contract shared by batch-solving strategies.
pub(super) struct BatchSolveContext<'a> {
    pub(super) current_slot: i32,
    pub(super) pending: &'a [Request],
    pub(super) shared_state: &'a SharedState,
    pub(super) assignment: &'a AssignmentPolicy<'a>,
    pub(super) solver_config: &'a SolverConfig,
    pub(super) simulation_config: &'a SimulationConfig,
    pub(super) recovery_config: &'a InfeasibilityConfig,
    pub(super) recovery_state: &'a RecoveryState,
    pub(super) verbose: bool,
    pub(super) carbon_forecast: &'a [f64],
    pub(super) flavour_duration_by_name: &'a HashMap<String, i32>,
}

/// Common result contract consumed by the worker commit/rollback coordinator.
pub(super) struct BatchSolveResult {
    pub(super) assignments: Vec<Assignment>,
    pub(super) context: SolveContext,
    pub(super) baseline_slot_counts: HashMap<i32, i32>,
}

/// Solver strategy interface. New stateless batch algorithms can implement
/// this contract and register a factory without changing worker dispatch.
pub(super) trait BatchSolverStrategy: Send + Sync {
    fn solve(&self, input: BatchSolveContext<'_>) -> BatchSolveResult;
}

struct DpBatchSolver;
struct GreedySingletonBatchSolver;

impl BatchSolverStrategy for DpBatchSolver {
    fn solve(&self, input: BatchSolveContext<'_>) -> BatchSolveResult {
        solve_dp(input)
    }
}

impl BatchSolverStrategy for GreedySingletonBatchSolver {
    fn solve(&self, input: BatchSolveContext<'_>) -> BatchSolveResult {
        solve_greedy_singleton(input)
    }
}

type BatchSolverFactory = fn() -> Box<dyn BatchSolverStrategy>;

fn build_dp_batch_solver() -> Box<dyn BatchSolverStrategy> {
    Box::new(DpBatchSolver)
}

fn build_greedy_singleton_batch_solver() -> Box<dyn BatchSolverStrategy> {
    Box::new(GreedySingletonBatchSolver)
}

const BATCH_SOLVER_REGISTRY: &[(&str, BatchSolverFactory)] = &[
    ("dp", build_dp_batch_solver),
    ("greedy_singleton", build_greedy_singleton_batch_solver),
];

/// Resolve the configured strategy once per worker. Unknown names retain the
/// historical DP fallback; known strategies are looked up in the registry.
pub(super) fn select_batch_solver(name: &str) -> Box<dyn BatchSolverStrategy> {
    BATCH_SOLVER_REGISTRY
        .iter()
        .find(|(registered_name, _)| name.trim().eq_ignore_ascii_case(registered_name))
        .map(|(_, factory)| factory())
        .unwrap_or_else(build_dp_batch_solver)
}

// ─── shared solve preamble (steps 1-4, used by both DP and greedy_singleton) ──

/// Everything the "solve" step of a batch worker needs, computed once from a
/// single consistent snapshot of shared state (Steps 1-4 of the pipeline).
/// Shared between `solve_dp` and `solve_greedy_singleton` so the snapshotting,
/// time-shifting, error-baseline, and global-error-constraint logic is not
/// duplicated between solver strategies.
struct PreparedSolve {
    pending_ids: HashSet<u64>,
    window_start: i32,
    window_end: i32,
    assignment_cap: i32,
    /// (request_id, capped_deadline) pairs to schedule — the batch's pending
    /// requests plus, when `dp_lock_future_assignments` is false, any movable
    /// future assignments re-joining the pool for joint re-planning.
    solve_requests: Vec<(u64, i32)>,
    /// request_id → (arrival_slot, capped_deadline), for converting solver
    /// results back into `Assignment`s.
    assignment_metadata: HashMap<u64, (i32, i32)>,
    baseline_slot_counts: HashMap<i32, i32>,
    error_baseline: ErrorBaseline,
    mock_pool_input: MockPool,
    global_stats: GlobalErrorStats,
    global_constraint_active: bool,
    /// Flavours allowed by the (possibly active) global error constraint.
    solver_flavours: Vec<Flavour>,
    /// Per-request (per-task) flavour overrides — see `prepare_solve`.
    request_flavours: HashMap<u64, Vec<Flavour>>,
    /// Local/window feasibility threshold (%) for this batch — see `prepare_solve`.
    effective_error_threshold: f64,
    /// Capacity tiers used for this batch solve; if a task-specific override was
    /// registered for any request in the batch, use that override; otherwise the
    /// global default from `Config` is used.
    effective_capacity_tiers: Vec<CapacityTier>,
}

fn prepare_solve(input: &BatchSolveContext<'_>) -> PreparedSolve {
    let current_slot = input.current_slot;
    let pending = input.pending;
    let shared_state = input.shared_state;
    let assignment = input.assignment;
    let solver = input.solver_config;
    let simulation = input.simulation_config;
    let recovery = input.recovery_config;
    let verbose = input.verbose;
    let recovery_state = input.recovery_state;

    let pending_ids: HashSet<u64> = pending.iter().map(|r| r.id).collect();

    // Deadline cap = end of error window.
    let window_start = (current_slot - assignment.error_window_past).max(0);
    let window_end =
        (current_slot + assignment.error_window_future).min(assignment.total_slots - 1);
    let assignment_cap = window_end;

    let cap_deadline = |d: i32| -> i32 {
        d.max(current_slot)
            .min(assignment_cap)
            .min(assignment.total_slots - 1)
    };

    // ── Step 1 (pre-solve): capture a consistent snapshot of shared state ────
    // All reads from shared state happen here, under a single lock acquisition.
    // The lock is released before the solver runs.
    let snapshot = shared_state.snapshot_for_solver();

    // ── Step 2: time-shifting ────────────────────────────────────────────────
    // If DP_LOCK_FUTURE_ASSIGNMENTS=True, future assignments are pinned as
    // baseline load.  If False, they join the pool for joint re-planning.
    let future_assignments = snapshot.get_future_assignments(current_slot);

    let mut solve_requests: Vec<(u64, i32)> = pending
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

    if solver.dp_lock_future_assignments {
        fixed_future = future_assignments.clone();
    } else {
        movable_future_ids = future_assignments.iter().map(|a| a.request_id).collect();
        for a in &future_assignments {
            let deadline = a
                .deadline_slot
                .unwrap_or_else(|| a.scheduled_slot.max(current_slot));
            let capped = cap_deadline(deadline);
            solve_requests.push((a.request_id, capped));
            assignment_metadata.insert(a.request_id, (a.arrival_slot.unwrap_or(0), capped));
        }
    }

    // Baseline counts from pinned future assignments.
    let mut baseline_slot_counts: HashMap<i32, i32> = HashMap::new();
    for a in &fixed_future {
        *baseline_slot_counts.entry(a.scheduled_slot).or_insert(0) += 1;
    }

    // ── Step 3: error baseline ──────────────────────────────────────────────
    // 3a. Real window error from snapshot.
    let ws = snapshot.get_window_error_stats(
        current_slot,
        assignment.error_window_past,
        assignment.error_window_future,
        &movable_future_ids,
    );
    let mut error_baseline = ErrorBaseline::new(ws.error_sum, ws.count as f64);

    // 3b. Decayed past extension (no-op when decay_slots == 0, which is the default).
    error_baseline = augment_with_decayed_past(
        current_slot,
        error_baseline,
        assignment,
        &snapshot,
        &movable_future_ids,
    );

    // 3c. Virtual prehistory for startup slots (disabled by default).
    error_baseline = augment_with_virtual_prehistory(
        current_slot,
        error_baseline,
        assignment,
        simulation,
        recovery,
    );

    // 3d. Infeasibility recovery: inject mock low-error requests to dilute the baseline.
    let (augmented_baseline, mock_pool_input) = apply_infeasibility_recovery(
        current_slot,
        error_baseline.clone(),
        assignment,
        simulation,
        recovery,
        &snapshot,
        recovery_state,
    );
    let error_baseline = augmented_baseline;

    // ── Step 4: global error constraint ────────────────────────────────────
    let global_stats = snapshot.get_global_error_stats();
    let mut solver_flavours = assignment.flavours.to_vec();
    let global_constraint_active;

    if assignment.global_error_constraint_enabled
        && global_stats.count > 0
        && global_stats.avg > assignment.max_error_threshold
    {
        global_constraint_active = true;
        if assignment.global_error_constraint_hard {
            let before = solver_flavours.len();
            solver_flavours.retain(|f| f.error <= assignment.max_error_threshold);
            if solver_flavours.is_empty() {
                // Safety: never remove all flavours.
                solver_flavours = assignment.flavours.to_vec();
            } else if verbose && solver_flavours.len() < before {
                println!(
                    "[Scheduler] ⚠ Global error constraint (HARD): \
                     global_avg={:.4}% > {:.2}% → {} flavours remaining",
                    global_stats.avg,
                    assignment.max_error_threshold,
                    solver_flavours.len()
                );
            }
        }
    } else {
        global_constraint_active = false;
    }

    // Per-request (per-task) flavour overrides: requests whose task was
    // dynamically registered (see `service::handlers::register_task`) carry
    // their own `flavours` list, resolved once at intake. Requests with no
    // override (empty `flavours`, e.g. CLI/simulation tools) fall back to
    // `solver_flavours`/`assignment.flavours` inside the solver, unaffected by this
    // map. The global error constraint above is task-agnostic by design
    // (it reflects overall scheduler error, not any one task) but must still
    // be honoured by these overrides too, so apply the same hard filter.
    let mut request_flavours: HashMap<u64, Vec<Flavour>> = pending
        .iter()
        .filter(|r| !r.flavours.is_empty())
        .map(|r| (r.id, r.flavours.clone()))
        .collect();
    if global_constraint_active && assignment.global_error_constraint_hard {
        for flavours in request_flavours.values_mut() {
            let filtered: Vec<Flavour> = flavours
                .iter()
                .cloned()
                .filter(|f| f.error <= assignment.max_error_threshold)
                .collect();
            if !filtered.is_empty() {
                *flavours = filtered;
            } // else: never remove all flavours for a request (same safety rule as solver_flavours).
        }
    }

    // Per-task local/window feasibility threshold: if any request in this
    // batch registered its own `max_error_threshold` (see
    // `service::handlers::register_task`), use the *strictest* (minimum) of
    // them for the whole batch solve — different tasks' calibrated flavours
    // can have very different error ranges (e.g. text_generation's may all
    // be 20%+, so the global 4% default would make it permanently
    // infeasible); this only affects the local/window check below, never
    // the (task-agnostic by design) global error constraint above, which
    // always uses `assignment.max_error_threshold`.
    let effective_error_threshold = pending
        .iter()
        .filter_map(|r| r.max_error_threshold)
        .fold(None::<f64>, |acc, t| Some(acc.map_or(t, |a: f64| a.min(t))))
        .unwrap_or(assignment.max_error_threshold);

    // Per-task capacity tiers: if any request in this batch registered its own
    // `capacity_tiers`, use them; otherwise fall back to the default from the
    // config. We keep an owned Vec here so the solver receives a slice with a
    // stable lifetime, instead of borrowing from `pending`.
    let effective_capacity_tiers = pending
        .iter()
        .find_map(|r| r.capacity_tiers.clone())
        .unwrap_or_else(|| assignment.capacity_tiers.to_vec());

    PreparedSolve {
        pending_ids,
        window_start,
        window_end,
        assignment_cap,
        solve_requests,
        assignment_metadata,
        baseline_slot_counts,
        error_baseline,
        mock_pool_input,
        global_stats,
        global_constraint_active,
        solver_flavours,
        request_flavours,
        effective_error_threshold,
        effective_capacity_tiers,
    }
}

// ─── core DP pipeline ─────────────────────────────────────────────────────────

fn solve_dp(input: BatchSolveContext<'_>) -> BatchSolveResult {
    let PreparedSolve {
        pending_ids,
        window_start,
        window_end,
        assignment_cap,
        solve_requests: dp_requests,
        assignment_metadata,
        baseline_slot_counts,
        error_baseline,
        mock_pool_input,
        global_stats,
        global_constraint_active,
        solver_flavours,
        request_flavours,
        effective_error_threshold,
        effective_capacity_tiers,
    } = prepare_solve(&input);

    let BatchSolveContext {
        current_slot,
        pending,
        shared_state: _,
        assignment,
        solver_config,
        simulation_config: _,
        recovery_config,
        recovery_state,
        verbose,
        carbon_forecast,
        flavour_duration_by_name: fdb,
    } = input;

    // ── Step 5: DP solve ────────────────────────────────────────────────────
    let effective_pruning = get_effective_pruning_mode(pending.len(), solver_config);
    let solver = build_solver(
        &solver_flavours,
        carbon_forecast,
        assignment,
        solver_config,
        &effective_pruning,
    );

    let base_counts_arr: Vec<i32> = (0..assignment.total_slots)
        .map(|s| baseline_slot_counts.get(&s).copied().unwrap_or(0))
        .collect();

    let dp_result = solver.solve_batch(SolveBatchInput {
        requests: &dp_requests,
        current_slot,
        capacity_multiplier: 1.0,
        capacity_tiers: &effective_capacity_tiers,
        baseline_slot_counts: &baseline_slot_counts,
        error_window_baseline: ErrorWindowBaseline {
            error_sum: error_baseline.error_sum,
            request_count: error_baseline.request_count,
        },
        max_error_threshold: Some(effective_error_threshold),
        error_window_past: assignment.error_window_past,
        error_window_future: assignment.error_window_future,
        assignment_max_slot: Some(assignment_cap),
        dynamic_mock_pool: mock_pool_input.clone(),
        request_flavours: &request_flavours,
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
        let cap_deadline = |d: i32| -> i32 {
            d.max(current_slot)
                .min(assignment_cap)
                .min(assignment.total_slots - 1)
        };
        // Greedy fallback: the error constraint is never relaxed/removed — on
        // infeasibility (even after mock-pool dilution, if the recovery mode
        // injects one) we go straight to the accurate-flavour/cheapest-slot
        // fallback below. There is no intermediate "retry DP without the
        // error threshold" step.
        //
        // Only the requests the DP left unscheduled are handed to the
        // fallback; requests it *did* place keep their (better) DP
        // assignment instead of being discarded and redone greedily too
        // (a single infeasible request no longer drags the whole batch
        // down to the greedy/most-accurate-flavour path).
        let unscheduled: Vec<(u64, i32)> = pending
            .iter()
            .filter(|r| !scheduled_pending_ids.contains(&r.id))
            .map(|r| (r.id, cap_deadline(r.deadline_slot)))
            .collect();
        if verbose {
            println!(
                "[Scheduler] ⚠ Infeasible ({}/{} pending covered): greedy fallback for {} request(s).",
                scheduled_pending_ids.len(),
                pending_ids.len(),
                unscheduled.len()
            );
        }
        let deadlines: Vec<i32> = unscheduled.iter().map(|(_, d)| *d).collect();

        // Fallback cost/capacity accounting must include slots the DP
        // already filled in this same batch, not just the pre-batch baseline.
        let mut fallback_base_counts = base_counts_arr.clone();
        for a in &dp_assignments {
            if pending_ids.contains(&a.request_id) {
                fallback_base_counts[a.slot as usize] += 1;
            }
        }

        let greedy_solver = build_solver(
            assignment.flavours,
            carbon_forecast,
            assignment,
            solver_config,
            "none",
        );
        let greedy = greedy_solver.greedy_fallback(
            &unscheduled,
            &deadlines,
            current_slot,
            assignment.capacity_tiers,
            &fallback_base_counts,
            &request_flavours,
        );
        dp_assignments.extend(greedy);
        solve_status = "ok_greedy_after_infeasible".to_string();
        solve_mode = "greedy_after_infeasible".to_string();
    }

    // Safety check: if not all pending are covered, signal infeasibility.
    let final_pending_covered: usize = dp_assignments
        .iter()
        .filter(|a| pending_ids.contains(&a.request_id))
        .count();

    if final_pending_covered != pending_ids.len() {
        if verbose {
            println!("[Scheduler] ⚠ Infeasible batch; retrying later.");
        }
        return BatchSolveResult {
            assignments: vec![],
            context: SolveContext {
                status: "infeasible".to_string(),
                mode: solve_mode,
                ..Default::default()
            },
            baseline_slot_counts: HashMap::new(),
        };
    }

    // ── Step 6: convert RequestAssignment → Assignment ──────────────────────
    let assignments: Vec<Assignment> = dp_assignments
        .iter()
        .map(|dp_a| {
            let (arrival, deadline) = assignment_metadata
                .get(&dp_a.request_id)
                .copied()
                .unwrap_or((0, 0));
            let dur = request_flavours
                .get(&dp_a.request_id)
                .and_then(|flavours| flavours.iter().find(|f| f.name == dp_a.flavour_name))
                .map(|f| f.duration)
                .unwrap_or_else(|| fdb.get(&dp_a.flavour_name).copied().unwrap_or(0));
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
    consume_mock_pool(
        current_slot,
        &solve_mode,
        mock_consumed,
        &recovery_config.recovery_mode,
        recovery_state,
    );

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
        recovery_mode: recovery_config.recovery_mode.clone(),
        solver_elapsed_ms: 0.0, // filled by the caller
    };

    BatchSolveResult {
        assignments,
        context: ctx,
        baseline_slot_counts,
    }
}

// ─── greedy singleton pipeline (online strategy, batch_size=1 only) ──────────

/// Online greedy-cheapest strategy, restricted to `batch_size=1`.
///
/// For its one pending request, exhaustively scans every `(slot, flavour)`
/// pair in `[current_slot, deadline]` and commits the cheapest one that
/// satisfies the local error window and global error constraint — the same
/// logic as the offline `greedy_cheapest` strategy (see
/// `bin/nshift/main.rs::run_greedy_cheapest`), but driven through the live
/// scheduler/`SharedState` instead of a single in-memory pass over a whole
/// scenario.
///
/// With exactly one pending request there is no combinatorial ordering
/// choice to make, so this exhaustive scan is already optimal for that one
/// decision — no DP state-space search is needed. That is why this is a
/// distinct, lighter "online alternative strategy" (grouped with Bandit/ACO)
/// rather than just "DP with batch_size=1": it skips the DP machinery
/// entirely, at the cost of never jointly re-planning already-scheduled
/// future assignments (it always treats them as pinned baseline load,
/// regardless of `dp_lock_future_assignments`).
///
/// Shares `prepare_solve`'s snapshot/error-baseline/global-constraint setup
/// with `solve_dp` so both strategies see the exact same feasibility rules;
/// it also shares `batch_worker_entry`'s rollback-checked commit path, since
/// — like DP — its cost model depends on accurate per-slot request counts
/// that a concurrent capacity-tier breach could invalidate.
fn solve_greedy_singleton(input: BatchSolveContext<'_>) -> BatchSolveResult {
    if input.verbose && input.pending.len() > 1 {
        println!(
            "[Scheduler] ⚠ greedy_singleton received a batch of {} pending requests; \
             it only supports batch_size=1 — scheduling them sequentially.",
            input.pending.len()
        );
    }

    let prep = prepare_solve(&input);
    let BatchSolveContext {
        current_slot,
        pending: _,
        shared_state: _,
        assignment,
        solver_config: _,
        simulation_config: _,
        recovery_config,
        recovery_state,
        verbose: _,
        carbon_forecast,
        flavour_duration_by_name: fdb,
    } = input;

    // ── Step 5: exhaustive greedy scan over (slot, flavour) ─────────────────
    let mut sorted_flavours: Vec<&Flavour> = prep.solver_flavours.iter().collect();
    sorted_flavours.sort_by_key(|f| f.duration);
    let fallback_flav = prep
        .solver_flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    let global_avg = if prep.global_stats.count > 0 {
        prep.global_stats.avg
    } else {
        0.0
    };
    let global_constraint_active = prep.global_constraint_active
        && assignment.global_error_constraint_hard
        && global_avg > assignment.max_error_threshold;

    // Local mutable slot counts, seeded from the baseline (pinned future
    // assignments) and updated as each request in `solve_requests` is
    // placed — for the common batch_size=1 case this loop runs once.
    let mut slot_count: HashMap<i32, i32> = prep.baseline_slot_counts.clone();
    let mut solved: Vec<RequestAssignment> = Vec::new();

    for &(request_id, deadline) in &prep.solve_requests {
        let (arrival, _) = prep
            .assignment_metadata
            .get(&request_id)
            .copied()
            .unwrap_or((current_slot, deadline));
        let start_slot = arrival.max(current_slot);

        let mut best: Option<(f64, i32, &Flavour)> = None;
        for slot in start_slot..=deadline {
            let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
            let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
            let mult = get_capacity_multiplier(assignment.capacity_tiers, position as i64);

            for flav in &sorted_flavours {
                // Global error constraint: retrospective (average error
                // *before* this request), matching solve_dp's step-function
                // behaviour rather than a per-candidate forward projection.
                if global_constraint_active && flav.error > assignment.max_error_threshold {
                    continue;
                }

                // Local error window is anchored to `arrival` (the request's
                // decision moment), not to the candidate `slot` being tried —
                // mirrors solve_dp, which centers the window on current_slot
                // regardless of where the request ends up being placed.
                let win_start = (arrival - assignment.error_window_past).max(0);
                let win_end =
                    (arrival + assignment.error_window_future).min(assignment.total_slots - 1);
                let mut win_sum = prep.error_baseline.error_sum + flav.error;
                let mut win_cnt = prep.error_baseline.request_count + 1.0;
                for a in &solved {
                    if a.slot >= win_start && a.slot <= win_end {
                        win_sum += a.error;
                        win_cnt += 1.0;
                    }
                }
                if win_cnt > 0.0 && win_sum / win_cnt > assignment.max_error_threshold {
                    continue;
                }

                let cost = ci * mult * flav.duration as f64 * assignment.carbon_cost_duration_scale;
                if best.map(|(c, _, _)| cost < c).unwrap_or(true) {
                    best = Some((cost, slot, flav));
                }
            }
        }

        // Commit the cheapest feasible pair; if none is feasible (all
        // flavours/slots violate the error window or global constraint),
        // fall back to the accurate (min-error) flavour at the earliest slot
        // — this guarantees every request is scheduled, so greedy_singleton
        // never returns "infeasible" to the caller.
        let (chosen_cost, chosen_slot, chosen_flav) = best.unwrap_or_else(|| {
            let ci = carbon_forecast
                .get(start_slot as usize)
                .copied()
                .unwrap_or(1.0);
            let position = *slot_count.get(&start_slot).unwrap_or(&0) + 1;
            let mult = get_capacity_multiplier(assignment.capacity_tiers, position as i64);
            let cost =
                ci * mult * fallback_flav.duration as f64 * assignment.carbon_cost_duration_scale;
            (cost, start_slot, fallback_flav)
        });

        *slot_count.entry(chosen_slot).or_insert(0) += 1;
        solved.push(RequestAssignment {
            request_id,
            flavour_name: chosen_flav.name.clone(),
            slot: chosen_slot,
            carbon_cost: chosen_cost,
            error: chosen_flav.error,
        });
    }

    let solve_status = "ok".to_string();
    let solve_mode = "greedy_singleton".to_string();

    // ── Step 6: convert RequestAssignment → Assignment ──────────────────────
    let assignments: Vec<Assignment> = solved
        .iter()
        .map(|ra| {
            let (arrival, deadline) = prep
                .assignment_metadata
                .get(&ra.request_id)
                .copied()
                .unwrap_or((0, 0));
            let dur = prep
                .request_flavours
                .get(&ra.request_id)
                .and_then(|flavours| flavours.iter().find(|f| f.name == ra.flavour_name))
                .map(|f| f.duration)
                .unwrap_or_else(|| fdb.get(&ra.flavour_name).copied().unwrap_or(0));
            Assignment::new(
                ra.request_id,
                ra.slot,
                ra.flavour_name.clone(),
                ra.carbon_cost,
                ra.error,
                dur,
                Some(arrival),
                Some(deadline),
            )
        })
        .collect();

    // Compute the modelled window average after this run (mirrors solve_dp).
    let mut modeled_error_sum = prep.error_baseline.error_sum;
    let mut modeled_count = prep.error_baseline.request_count;
    let mut mock_remaining = prep.mock_pool_input.initial_count;
    let mock_err = prep.mock_pool_input.error_per_request;

    for a in &assignments {
        if a.scheduled_slot >= prep.window_start && a.scheduled_slot <= prep.window_end {
            modeled_error_sum += a.error;
            modeled_count += 1.0;
            if mock_remaining > 0 && mock_err > 0.0 {
                modeled_error_sum -= mock_err;
                modeled_count = (modeled_count - 1.0).max(0.0);
                mock_remaining -= 1;
            }
        }
    }
    let mock_consumed = (prep.mock_pool_input.initial_count - mock_remaining).max(0);
    consume_mock_pool(
        current_slot,
        &solve_mode,
        mock_consumed,
        &recovery_config.recovery_mode,
        recovery_state,
    );

    let ctx = SolveContext {
        status: solve_status,
        mode: solve_mode,
        new_assignments: prep.pending_ids.len(),
        total_assignments: assignments.len(),
        global_error_before: prep.global_stats.avg,
        global_error_count_before: prep.global_stats.count,
        global_error_constraint_active: prep.global_constraint_active,
        modeled_window_avg_after: if modeled_count > 0.0 {
            modeled_error_sum / modeled_count
        } else {
            0.0
        },
        window_start_slot: prep.window_start,
        window_end_slot: prep.window_end,
        mock_recovery_consumed: mock_consumed,
        recovery_mode: recovery_config.recovery_mode.clone(),
        solver_elapsed_ms: 0.0, // filled by the caller
    };

    BatchSolveResult {
        assignments,
        context: ctx,
        baseline_slot_counts: prep.baseline_slot_counts,
    }
}

// ─── misc helpers ─────────────────────────────────────────────────────────────

pub(super) fn get_effective_pruning_mode(batch_size: usize, solver: &SolverConfig) -> String {
    let threshold = solver.dp_pruning_min_batch_size;
    if threshold == 0 || batch_size < threshold {
        "none".to_string()
    } else {
        solver.dp_pruning_method.trim().to_lowercase()
    }
}

fn build_solver(
    flavours: &[Flavour],
    carbon_forecast: &[f64],
    assignment: &AssignmentPolicy<'_>,
    solver_config: &SolverConfig,
    pruning: &str,
) -> DpSolver {
    let mut solver =
        DpSolver::new(solver_config, assignment).with_carbon_forecast(carbon_forecast.to_vec());
    solver.flavours = flavours.to_vec();
    solver.pruning = pruning.to_string();
    solver
}
