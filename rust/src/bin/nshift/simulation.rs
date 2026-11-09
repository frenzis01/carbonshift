//! Streaming scheduler simulation and baseline execution for nshift.

use super::*;
use carbonshift_rs::scheduler::AssignmentCommitObserver;
use std::collections::HashMap;
use std::io::Write;
use std::sync::Mutex;

#[derive(Debug, Clone)]
struct PendingPerRequestContext {
    included_in_batch_slot: i32,
    batch_sequence: u64,
    assignment_solver_status: String,
    fallback_assigned: bool,
    late_assigned: bool,
}

/// Nshift-only metadata that is needed to form a notebook report row after
/// the assignment itself becomes immutable. Context is removed as its slot is
/// finalized, so this map covers only work that is not settled yet.
#[derive(Default)]
struct PendingPerRequestContexts {
    by_request_id: Mutex<HashMap<u64, PendingPerRequestContext>>,
}

impl PendingPerRequestContexts {
    fn get(&self, request_id: u64) -> Option<PendingPerRequestContext> {
        self.by_request_id.lock().unwrap().get(&request_id).cloned()
    }

    fn remove(&self, request_id: u64) {
        self.by_request_id.lock().unwrap().remove(&request_id);
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.by_request_id.lock().unwrap().len()
    }
}

impl AssignmentCommitObserver for PendingPerRequestContexts {
    fn assignments_committed(
        &self,
        current_slot: i32,
        batch_sequence: u64,
        solver_status: &str,
        fallback_request_ids: &HashSet<u64>,
        late_request_ids: &HashSet<u64>,
        assignments: &[carbonshift_rs::types::Assignment],
    ) {
        let mut contexts = self.by_request_id.lock().unwrap();
        for assignment in assignments {
            // Recommit replaces the context too: the final report must describe
            // the latest committed placement, not an earlier proposal.
            contexts.insert(
                assignment.request_id,
                PendingPerRequestContext {
                    included_in_batch_slot: current_slot,
                    batch_sequence,
                    assignment_solver_status: solver_status.to_owned(),
                    fallback_assigned: fallback_request_ids.contains(&assignment.request_id),
                    late_assigned: late_request_ids.contains(&assignment.request_id),
                },
            );
        }
    }
}

/// Writes final per-request rows in settled-slot order and retains only
/// run-level/slot-level aggregates after each settled row has been flushed.
///
/// Sorts each slot’s assignments by request ID, computes canonical global-capacity
/// prices, streams rows to per_request.csv, per_request.json, and assignments_runtime.csv,
/// flushes before eviction, and updates summary and per-slot error aggregates.
struct SettledPerRequestWriter {
    csv: csv::Writer<std::fs::File>,
    json: std::io::BufWriter<std::fs::File>,
    runtime_csv: csv::Writer<std::fs::File>,
    runtime_timestamp: String,
    first_json_row: bool,
    summary: PerRequestSummaryAccumulator,
    slot_errors: Vec<SlotErrorAggregate>,
    next_slot: i32,
    contexts: Arc<PendingPerRequestContexts>,
}

impl SettledPerRequestWriter {
    fn create(
        run_dir: &Path,
        total_slots: i32,
        contexts: Arc<PendingPerRequestContexts>,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        std::fs::create_dir_all(run_dir)?;
        let mut csv = csv::Writer::from_path(run_dir.join("per_request.csv"))?;
        csv.write_record([
            "request_id",
            "qos_profile_id",
            "arrival_time",
            "arrival_slot",
            "deadline_slot",
            "included_in_batch_slot",
            "batch_sequence",
            "scheduled_slot",
            "queue_wait_slots",
            "queue_wait_seconds",
            "final_wait_slots",
            "final_wait_seconds",
            "flavour_name",
            "carbon_cost",
            "assignment_solver_status",
            "fallback_assigned",
            "late_assigned",
        ])?;

        // Keep the existing JSON wrapper/schema while streaming one JSON row at
        // a time. This avoids building the old full `Vec<Value>` in RAM.
        let mut json =
            std::io::BufWriter::new(std::fs::File::create(run_dir.join("per_request.json"))?);
        json.write_all(br#"{"rows":["#)?;

        let runtime_timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs_f64()
            .to_string();
        let mut runtime_csv = csv::Writer::from_path(run_dir.join("assignments_runtime.csv"))?;
        runtime_csv.write_record([
            "request_id",
            "qos_profile_id",
            "scheduled_slot",
            "flavour",
            "carbon_cost",
            "assignment_time",
        ])?;
        runtime_csv.flush()?;
        Ok(Self {
            csv,
            json,
            runtime_csv,
            runtime_timestamp,
            first_json_row: true,
            summary: PerRequestSummaryAccumulator::default(),
            slot_errors: vec![SlotErrorAggregate::default(); total_slots.max(0) as usize],
            next_slot: 0,
            contexts,
        })
    }

    fn finalize_through(
        &mut self,
        shared_state: &SharedState,
        settled_through_slot: i32,
        carbon_forecast: &[f64],
        tiers: &[carbonshift_rs::types::CapacityTier],
        duration_scale: f64,
        slot_duration_seconds: f64,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        while self.next_slot <= settled_through_slot {
            let slot = self.next_slot;
            let carbon_intensity = *carbon_forecast.get(slot as usize).ok_or_else(|| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("missing carbon forecast for settled slot {slot}"),
                )
            })?;
            let mut assignments = shared_state.get_requests_in_slot(slot);
            assignments.sort_by_key(|assignment| assignment.request_id);

            let mut rows = Vec::with_capacity(assignments.len());
            for (index, assignment) in assignments.iter().enumerate() {
                let context = self.contexts.get(assignment.request_id).ok_or_else(|| {
                    std::io::Error::other(format!(
                        "missing nshift reporting context for request {}",
                        assignment.request_id
                    ))
                })?;
                let position = i64::try_from(index + 1)?;
                let multiplier = get_capacity_multiplier(tiers, position);
                let arrival_slot = assignment.arrival_slot.unwrap_or(slot);
                let deadline_slot = assignment.deadline_slot.unwrap_or(slot);
                let queue_wait_slots = (context.included_in_batch_slot - arrival_slot).max(0);
                let final_wait_slots = (slot - arrival_slot).max(0);
                rows.push(PerRequest {
                    request_id: assignment.request_id,
                    qos_profile_id: assignment.qos_profile_id.to_string(),
                    arrival_time: arrival_slot as f64 * slot_duration_seconds,
                    arrival_slot,
                    deadline_slot,
                    included_in_batch_slot: context.included_in_batch_slot,
                    batch_sequence: context.batch_sequence,
                    scheduled_slot: slot,
                    queue_wait_slots,
                    queue_wait_seconds: queue_wait_slots as f64 * slot_duration_seconds,
                    final_wait_slots,
                    final_wait_seconds: final_wait_slots as f64 * slot_duration_seconds,
                    flavour_name: assignment.flavour_name.clone(),
                    error: assignment.error,
                    carbon_cost: carbon_intensity
                        * multiplier
                        * assignment.flavour_duration
                        * duration_scale,
                    assignment_solver_status: context.assignment_solver_status,
                    fallback_assigned: context.fallback_assigned,
                    late_assigned: context.late_assigned,
                    lateness_slots: (slot - deadline_slot).max(0),
                });
            }

            // Flush all report views for this slot before releasing the
            // assignment rows needed to derive them. A write error is fatal to
            // this run; the caller must not continue with partially missing
            // output and evicted state.
            self.write_rows(&rows)?;
            self.flush_slot()?;

            for assignment in &assignments {
                let removed = shared_state
                    .evict_settled_assignment(assignment.request_id, slot)
                    .map_err(std::io::Error::other)?;
                if removed.is_none() {
                    return Err(std::io::Error::other(format!(
                        "settled assignment {} disappeared before eviction",
                        assignment.request_id
                    ))
                    .into());
                }
            }
            for row in &rows {
                self.contexts.remove(row.request_id);
                self.record_summary(row)?;
            }
            self.next_slot = self.next_slot.checked_add(1).ok_or_else(|| {
                std::io::Error::other("settled-slot finalizer overflowed slot index")
            })?;
        }
        Ok(())
    }

    fn write_rows(
        &mut self,
        rows: &[PerRequest],
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        for row in rows {
            self.csv.write_record([
                row.request_id.to_string(),
                row.qos_profile_id.clone(),
                row.arrival_time.to_string(),
                row.arrival_slot.to_string(),
                row.deadline_slot.to_string(),
                row.included_in_batch_slot.to_string(),
                row.batch_sequence.to_string(),
                row.scheduled_slot.to_string(),
                row.queue_wait_slots.to_string(),
                row.queue_wait_seconds.to_string(),
                row.final_wait_slots.to_string(),
                row.final_wait_seconds.to_string(),
                row.flavour_name.clone(),
                row.carbon_cost.to_string(),
                row.assignment_solver_status.clone(),
                row.fallback_assigned.to_string(),
                row.late_assigned.to_string(),
            ])?;
            if !self.first_json_row {
                self.json.write_all(b",")?;
            }
            serde_json::to_writer(&mut self.json, &per_request_json_row(row))?;
            self.first_json_row = false;
            self.runtime_csv.write_record([
                row.request_id.to_string(),
                row.qos_profile_id.clone(),
                row.scheduled_slot.to_string(),
                row.flavour_name.clone(),
                row.carbon_cost.to_string(),
                self.runtime_timestamp.clone(),
            ])?;
        }
        Ok(())
    }

    fn flush_slot(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        self.csv.flush()?;
        self.json.flush()?;
        self.runtime_csv.flush()?;
        Ok(())
    }

    fn record_summary(
        &mut self,
        row: &PerRequest,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        self.summary.record(row);
        let slot_index = usize::try_from(row.scheduled_slot)?;
        let aggregate = self.slot_errors.get_mut(slot_index).ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "PerRequest slot {} is outside the configured horizon",
                    row.scheduled_slot
                ),
            )
        })?;
        aggregate.error_sum += row.error;
        aggregate.count += 1;
        Ok(())
    }

    fn write_late_rows(
        &mut self,
        rows: &mut [PerRequest],
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        rows.sort_by_key(|row| (row.scheduled_slot, row.request_id));
        self.write_rows(rows)?;
        self.flush_slot()?;
        for row in rows {
            self.record_summary(row)?;
        }
        Ok(())
    }

    fn finish(
        mut self,
    ) -> Result<
        (PerRequestSummaryAccumulator, Vec<SlotErrorAggregate>),
        Box<dyn std::error::Error + Send + Sync>,
    > {
        self.json.write_all(b"]}")?;
        self.flush_slot()?;
        Ok((self.summary, self.slot_errors))
    }
}

fn per_request_json_row(row: &PerRequest) -> serde_json::Value {
    serde_json::json!({
        "request_id": row.request_id,
        "qos_profile_id": row.qos_profile_id,
        "arrival_time": row.arrival_time,
        "arrival_slot": row.arrival_slot,
        "deadline_slot": row.deadline_slot,
        "included_in_batch_slot": row.included_in_batch_slot,
        "batch_sequence": row.batch_sequence,
        "scheduled_slot": row.scheduled_slot,
        "queue_wait_slots": row.queue_wait_slots,
        "queue_wait_seconds": row.queue_wait_seconds,
        "final_wait_slots": row.final_wait_slots,
        "final_wait_seconds": row.final_wait_seconds,
        "flavour_name": row.flavour_name,
        "carbon_cost": row.carbon_cost,
        "assignment_solver_status": row.assignment_solver_status,
        "fallback_assigned": row.fallback_assigned,
        "late_assigned": row.late_assigned,
    })
}

/// Greedy baseline: assign each request immediately on arrival to the "best"
/// flavour (lowest error; mirrors Python's `run_greedy_baseline`).
pub(super) fn run_greedy_baseline(
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, RunSummary) {
    let flav = assignment
        .flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("assignment policy must have at least one flavour");
    let slot_dur = slot_duration_seconds;
    let carbon = &scenario.carbon_forecast;
    let by_slot = scenario.requests_by_slot();
    let tiers = assignment.capacity_tiers;
    let scale = assignment.carbon_cost_duration_scale;

    let mut per_req: Vec<PerRequest> = Vec::new();
    let mut seq: u64 = 0;

    for (slot, requests) in by_slot.iter().enumerate() {
        if requests.is_empty() {
            continue;
        }
        let ci = carbon.get(slot).copied().unwrap_or(1.0);
        let mut before_count: i64 = 0;
        for req in requests {
            seq += 1;
            let position = before_count + 1;
            let mult = get_capacity_multiplier(tiers, position);
            let cost = ci * mult * flav.duration * scale;
            before_count = position;
            per_req.push(PerRequest {
                request_id: req.id,
                qos_profile_id: QosProfileId::default_profile().to_string(),
                arrival_time: req.arrival_slot as f64 * slot_dur,
                arrival_slot: req.arrival_slot,
                deadline_slot: req.deadline_slot,
                included_in_batch_slot: slot as i32,
                batch_sequence: seq,
                scheduled_slot: slot as i32,
                queue_wait_slots: 0,
                queue_wait_seconds: 0.0,
                final_wait_slots: 0,
                final_wait_seconds: 0.0,
                flavour_name: flav.name.clone(),
                error: flav.error,
                carbon_cost: cost,
                assignment_solver_status: "ok".to_string(),
                fallback_assigned: false,
                late_assigned: false,
                lateness_slots: 0,
            });
        }
    }

    let total_requests: usize = by_slot.iter().map(|s| s.len()).sum();
    let solver_timings = SolverTimingSummary::zero_elapsed_samples(total_requests);
    let summary = compute_summary(
        "greedy_baseline_immediate",
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        solver_timings,
        total_requests,
        &flav.name,
        flav.duration,
        flav.error,
        0,   // peak_concurrent_workers
        0.0, // avg_concurrent_workers
    );
    (per_req, summary)
}

/// Greedy late scheduling: assign pending requests that remained unprocessed
/// after the run ended.
///
/// `scheduled_slot` is set to `deadline_slot` (clamped to [arrival_slot, total_slots-1])
/// so that the carbon cost is computed at the slot the request *should* have occupied.
///
/// `lateness_slots` = (total_slots-1) - deadline_slot, i.e. how far past the deadline
/// the request was still sitting unprocessed when the run ended. This is always >= 0
/// for drained requests because their deadline has already passed by end-of-run.
pub(super) fn schedule_late_requests(
    remaining: Vec<carbonshift_rs::types::Request>,
    carbon_forecast: &[f64],
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
) -> Vec<PerRequest> {
    if remaining.is_empty() {
        return Vec::new();
    }

    let slot_dur = slot_duration_seconds;
    let tiers = assignment.capacity_tiers;
    let scale = assignment.carbon_cost_duration_scale;
    let total_slots = assignment.total_slots;
    let fallback_flav = assignment
        .flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    let mut slot_count: HashMap<i32, i64> = HashMap::new();
    let mut out = Vec::with_capacity(remaining.len());

    let mut sorted = remaining;
    sorted.sort_by_key(|r| (r.deadline_slot, r.arrival_slot, r.id));

    for req in sorted {
        let sched_slot = req.deadline_slot.max(req.arrival_slot).min(total_slots - 1);
        let position = slot_count.get(&sched_slot).copied().unwrap_or(0) + 1;
        let ci = carbon_forecast
            .get(sched_slot as usize)
            .copied()
            .unwrap_or(1.0);
        let mult = get_capacity_multiplier(tiers, position);
        let cost = ci * mult * fallback_flav.duration * scale;
        // Lateness = how many slots past the deadline the request sat unprocessed.
        // We measure from the end of the run (total_slots-1), not from sched_slot,
        // because all drained requests were unprocessed until the run ended.
        let lateness = ((total_slots - 1) - req.deadline_slot).max(0);
        let queue_wait = (sched_slot - req.arrival_slot).max(0);

        *slot_count.entry(sched_slot).or_insert(0) += 1;

        out.push(PerRequest {
            request_id: req.id,
            qos_profile_id: QosProfileId::default_profile().to_string(),
            arrival_time: req.arrival_slot as f64 * slot_dur,
            arrival_slot: req.arrival_slot,
            deadline_slot: req.deadline_slot,
            included_in_batch_slot: sched_slot,
            batch_sequence: 0,
            scheduled_slot: sched_slot,
            queue_wait_slots: queue_wait,
            queue_wait_seconds: queue_wait as f64 * slot_dur,
            final_wait_slots: queue_wait,
            final_wait_seconds: queue_wait as f64 * slot_dur,
            flavour_name: fallback_flav.name.clone(),
            error: fallback_flav.error,
            carbon_cost: cost,
            assignment_solver_status: "late".to_string(),
            fallback_assigned: true,
            late_assigned: true,
            lateness_slots: lateness,
        });
    }
    out
}

pub(super) fn run_single_n(
    scenario: &Scenario,
    base_cfg: &Config,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    run_dir: &Path,
    verbose: bool,
) -> (RunSummary, Vec<PerTimeslot>) {
    // Build per-run config.
    let mut cfg = base_cfg.clone();
    cfg.solver.batch_size = batch_size;
    cfg.logging.verbose = verbose;
    // For fast simulation: use skip_empty_slots=true with slot_speed_scale=1.0.
    // The skip mechanism advances the virtual clock when a slot is empty, so the
    // scheduler races through the scenario without waiting for real time.
    // Do NOT set slot_speed_scale=0 here: with 0, the wall clock advances faster
    // than 1 virtual-slot/ms, causing the monitor to exit before all requests are
    // scheduled (the scheduler can't keep up with a 1ms/slot clock).
    if !realtime_slots {
        cfg.simulation.skip_empty_slots = true;
        cfg.simulation.slot_speed_scale = 1.0;
    } else {
        cfg.simulation.skip_empty_slots = false;
        cfg.simulation.slot_speed_scale = realtime_speed_scale;
    }
    // Scheduler counters supply summary timing; per-batch CSV rows only add
    // string-map allocations and file opens to this high-volume path.
    cfg.logging.enable_solver_logging = false;
    cfg.simulation.total_requests = scenario.requests.len();

    let cfg = Arc::new(cfg);

    std::fs::create_dir_all(run_dir).unwrap();
    let report_contexts = Arc::new(PendingPerRequestContexts::default());
    let mut per_request_writer =
        SettledPerRequestWriter::create(run_dir, cfg.total_slots, report_contexts.clone())
            .unwrap_or_else(|error| panic!("cannot create settled PerRequest output: {error}"));
    let mut finalized_through_slot = -1;

    let shared_state = SharedState::new();
    let ml = Arc::new(MetricsLogger::new(false, String::new(), String::new()));

    let by_slot = scenario.requests_by_slot();
    let generator_cfg = RequestGeneratorConfig::from_config(&cfg);
    let mut generator =
        RequestGenerator::new_from_scenario(shared_state.clone(), generator_cfg, by_slot);
    let mut sched = BatchScheduler::new_with_parallel_profile_batches_and_observer(
        shared_state.clone(),
        cfg.clone(),
        ml,
        Arc::new(RwLock::new(scenario.carbon_forecast.clone())),
        report_contexts.clone(),
    );

    sched.start();
    generator.start();

    // Phase 1: wait until the generator has finished emitting every slot.
    // `generator_processed_slot()` is the authoritative "generator done" signal
    // (set once per slot, right after that slot's requests are added). The
    // virtual clock (`virtual_elapsed_secs()`) is advanced independently by the
    // scheduler and must NOT be used as this gate: the generator now applies
    // backpressure (waiting for the scheduler to drain each slot before moving
    // on, see `generator.rs::wait_for_drain`), so under heavy load it can fall
    // far behind the virtual clock. Waiting on the clock alone would let this
    // phase end while the generator is still mid-scenario, and the fixed-size
    // drain timeout below would then cut it off, silently dropping every
    // not-yet-emitted slot's requests.
    let last_slot = cfg.total_slots - 1;
    loop {
        std::thread::sleep(std::time::Duration::from_millis(50));
        let settled_through_slot = shared_state.get_settled_through_slot();
        if settled_through_slot > finalized_through_slot {
            per_request_writer
                .finalize_through(
                    &shared_state,
                    settled_through_slot,
                    &scenario.carbon_forecast,
                    &base_cfg.capacity_tiers,
                    base_cfg.carbon_cost_duration_scale,
                    base_cfg.slot_duration_seconds,
                )
                .unwrap_or_else(|error| panic!("cannot finalize settled PerRequest rows: {error}"));
            finalized_through_slot = settled_through_slot;
        }
        if shared_state.generator_processed_slot() >= last_slot {
            break;
        }
    }

    // Phase 2: let the scheduler drain the remaining pending queue.
    // Requests may still be waiting (or mid-solve) when Phase 1 ends, since
    // Phase 1 only guarantees the generator has *emitted* every slot, not that
    // the scheduler has finished processing them. We keep the scheduler
    // running until pending==0 AND every worker has finished.
    let drain_deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    loop {
        let stats = sched.get_statistics();
        let pending = shared_state.get_pending_count();
        let settled_through_slot = shared_state.get_settled_through_slot();
        if settled_through_slot > finalized_through_slot {
            per_request_writer
                .finalize_through(
                    &shared_state,
                    settled_through_slot,
                    &scenario.carbon_forecast,
                    &base_cfg.capacity_tiers,
                    base_cfg.carbon_cost_duration_scale,
                    base_cfg.slot_duration_seconds,
                )
                .unwrap_or_else(|error| panic!("cannot finalize settled PerRequest rows: {error}"));
            finalized_through_slot = settled_through_slot;
        }
        if (pending == 0 && stats.active_batch_workers == 0)
            || std::time::Instant::now() > drain_deadline
        {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }

    generator.stop();
    sched.stop();
    let sched_stats = sched.get_statistics();

    // `stop` has joined the scheduler and drained every solver worker, so no
    // later batch can write into the final slot. Publish and finalize the
    // remaining horizon explicitly; the regular watermark is conservative
    // and normally remains one slot behind the active clock.
    shared_state.advance_settled_through_slot(last_slot);
    per_request_writer
        .finalize_through(
            &shared_state,
            last_slot,
            &scenario.carbon_forecast,
            &base_cfg.capacity_tiers,
            base_cfg.carbon_cost_duration_scale,
            base_cfg.slot_duration_seconds,
        )
        .unwrap_or_else(|error| panic!("cannot finalize final PerRequest rows: {error}"));

    let total_received = shared_state.get_statistics().total_received as usize;
    let solver_timing_summary = SolverTimingSummary::from_scheduler_stats(
        sched_stats.solver_runs as usize,
        sched_stats.min_solver_elapsed_ms,
        sched_stats.max_solver_elapsed_ms,
        sched_stats.avg_solver_ms_per_batch,
    );
    // Drain any requests that never made it into a batch (e.g., deadline
    // expired while waiting in the pending queue) and schedule them late.
    let remaining = shared_state.drain_pending_requests();
    if !remaining.is_empty() {
        let mut late = schedule_late_requests(
            remaining,
            &scenario.carbon_forecast,
            &base_cfg.assignment_policy(),
            base_cfg.slot_duration_seconds,
        );
        per_request_writer
            .write_late_rows(&mut late)
            .unwrap_or_else(|error| panic!("cannot write late PerRequest rows: {error}"));
    }
    let (request_summary, slot_errors) = per_request_writer
        .finish()
        .unwrap_or_else(|error| panic!("cannot finalize PerRequest output files: {error}"));
    let exec_mode = if cfg.solver.solver_strategy == "dp" || cfg.solver.solver_strategy.is_empty() {
        "nshift_dp".to_string()
    } else {
        format!("nshift_{}", cfg.solver.solver_strategy)
    };
    let summary = compute_summary_from_accumulators(
        &exec_mode,
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        request_summary,
        solver_timing_summary,
        total_received,
        "",
        0.0,
        0.0,
        sched_stats.peak_concurrent_workers,
        sched_stats.avg_concurrent_workers,
    );
    let per_timeslot = compute_per_timeslot_from_aggregates(
        &slot_errors,
        cfg.error_window_past,
        cfg.error_window_future,
    );

    // Print a definitive final progress line overwriting whatever partial
    // line the main_loop left behind.  This runs after late scheduling so
    // the numbers are always 100%.
    if !cfg.logging.verbose {
        let total = scenario.requests.len();
        let scheduled = summary.reqs_scheduled;
        let pct = if total > 0 {
            scheduled as f64 / total as f64 * 100.0
        } else {
            0.0
        };
        println!(
            "\r  [N={:2}] Scheduled {:>6}/{:<6} ({:5.1}%)  Received: {:>6}",
            batch_size, scheduled, total, pct, total_received
        );
    }

    (summary, per_timeslot)
}

#[cfg(test)]
mod tests {
    use super::*;
    use carbonshift_rs::engine::qos::QosProfileId;
    use carbonshift_rs::types::{Assignment, CapacityTier};

    fn test_assignment(id: u64, duration: f64, error: f64) -> Assignment {
        Assignment::new_for_profile(
            id,
            0,
            "Fast".to_owned(),
            0.0,
            error,
            duration,
            Some(0),
            Some(1),
            QosProfileId::default_profile(),
        )
    }

    fn expected_per_request(assignment: &Assignment) -> PerRequest {
        PerRequest {
            request_id: assignment.request_id,
            qos_profile_id: assignment.qos_profile_id.to_string(),
            arrival_time: 0.0,
            arrival_slot: 0,
            deadline_slot: 1,
            included_in_batch_slot: 0,
            batch_sequence: 7,
            scheduled_slot: 0,
            queue_wait_slots: 0,
            queue_wait_seconds: 0.0,
            final_wait_slots: 0,
            final_wait_seconds: 0.0,
            flavour_name: assignment.flavour_name.clone(),
            error: assignment.error,
            carbon_cost: 0.0,
            assignment_solver_status: "ok".to_owned(),
            fallback_assigned: assignment.request_id == 2,
            late_assigned: false,
            lateness_slots: 0,
        }
    }

    #[test]
    fn settled_writer_matches_whole_run_canonical_pricing_and_evicts_rows() {
        let state = SharedState::new();
        let assignments = vec![test_assignment(2, 20.0, 2.0), test_assignment(1, 10.0, 1.0)];
        state.add_assignments(assignments.clone());
        state.advance_settled_through_slot(0);

        let tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 3.0,
            },
        ];
        let forecast = [50.0];
        let scale = 1.0;
        let context = Arc::new(PendingPerRequestContexts::default());
        context.assignments_committed(
            0,
            7,
            "ok",
            &HashSet::from([2]),
            &HashSet::new(),
            &assignments,
        );

        let directory = std::env::temp_dir().join(format!(
            "nshift-settled-per-request-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut writer = SettledPerRequestWriter::create(&directory, 1, context.clone()).unwrap();

        let mut expected = assignments
            .iter()
            .map(expected_per_request)
            .collect::<Vec<_>>();
        let original_assignments: HashMap<_, _> = assignments
            .iter()
            .map(|assignment| (assignment.request_id, assignment.clone()))
            .collect();
        super::super::recompute_carbon_costs(
            &mut expected,
            &original_assignments,
            &forecast,
            &tiers,
            scale,
        );

        writer
            .finalize_through(&state, 0, &forecast, &tiers, scale, 60.0)
            .unwrap();
        assert!(state.get_current_assignments().is_empty());
        assert_eq!(context.len(), 0);

        let (summary, slot_errors) = writer.finish().unwrap();
        assert_eq!(summary.count, 2);
        assert_eq!(slot_errors[0].count, 2);
        assert_eq!(slot_errors[0].error_sum, 3.0);

        let mut reader = csv::Reader::from_path(directory.join("per_request.csv")).unwrap();
        let headers = reader.headers().unwrap().clone();
        let column = |name: &str| headers.iter().position(|header| header == name).unwrap();
        let request_id_column = column("request_id");
        let cost_column = column("carbon_cost");
        let batch_sequence_column = column("batch_sequence");
        let status_column = column("assignment_solver_status");
        let fallback_column = column("fallback_assigned");
        let records = reader.records().map(Result::unwrap).collect::<Vec<_>>();
        assert_eq!(
            records
                .iter()
                .map(|record| record[request_id_column].parse::<u64>().unwrap())
                .collect::<Vec<_>>(),
            vec![1, 2]
        );
        assert_eq!(records[0].get(batch_sequence_column), Some("7"));
        assert_eq!(records[0].get(status_column), Some("ok"));
        assert_eq!(records[0].get(fallback_column), Some("false"));
        assert_eq!(records[1].get(fallback_column), Some("true"));
        let actual: HashMap<u64, f64> = records
            .iter()
            .map(|record| {
                (
                    record[request_id_column].parse().unwrap(),
                    record[cost_column].parse().unwrap(),
                )
            })
            .collect();
        let expected: HashMap<u64, f64> = expected
            .into_iter()
            .map(|row| (row.request_id, row.carbon_cost))
            .collect();
        assert_eq!(actual, expected);

        let json: serde_json::Value =
            serde_json::from_slice(&std::fs::read(directory.join("per_request.json")).unwrap())
                .unwrap();
        assert_eq!(json["rows"].as_array().unwrap().len(), 2);
        std::fs::remove_dir_all(directory).unwrap();
    }
}

// ─── main ─────────────────────────────────────────────────────────────────────
